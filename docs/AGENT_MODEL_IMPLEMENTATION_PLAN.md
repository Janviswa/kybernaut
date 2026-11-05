# Kybernaut Agent and Model Implementation Plan

**Status: audit and plan only.** No autonomous-agent or machine-learning
model implementation is authorized by this document. This plan describes
the repository state inspected on 2026-10-22 and proposes a small,
testable, service-led vertical slice before adding new autonomy.

**Phase 4.18 current-state note:** Background application anomaly monitoring
is enabled by default unless explicitly disabled, and operator-initiated APIs
remain supported. The monitor persists incident state and dispatches
authorized handoffs; remediation remains behind existing authorization and
approval safeguards. `GET /monitor/status` exposes only available operational
state, while `/health` remains API liveness and does not establish monitor
health. This status surface is not proof of production reliability.

**Phase F.2 implementation note:** The selected workflow boundary now uses
the strict Pydantic contract in
[incident_contract.py](../app/models/incident_contract.py). The integrated
response graph returns the validated contract alongside its existing response
fields; it carries source availability, evidence-backed classification,
diagnosis gaps, decision/approval/execution state, verification status, and
audit event IDs. Contract evidence is bounded and reports when truncation
occurred. The advisory workflow now adds the same versioned contract and a
contract status/error code while retaining its prior response fields. Its
generated correlation ID is scoped to the contract; the legacy response has
no separate incident-ID field. Contract validation failure is reported and
does not block advisory RCA or deterministic policy evaluation.
Classifier diagnosis findings are intentionally excluded from benchmark
category selection because the supported service findings do not establish
one of the Kubernetes/telemetry categories; they remain available to
deterministic decision policy. The advisory policy does not use LLM RCA output
to select or relax actions; approval and execution remain outside this graph.

**Phase F.4/F.5 evidence audit:** Both workflow mappers use the same source
timestamp/status normalizer. The integrated mapper preserves a supplied
diagnosis timestamp, but diagnosis does not aggregate the per-query timestamps
into one contract `observed_at`. The `diagnose_service` path now retains each
Prometheus result timestamp separately in additive `evidence_timestamps`
metadata: the direct `up` sample timestamp is labeled as a Prometheus sample
timestamp, while the 5xx-rate and P95 expression result timestamps are labeled
as instant-query result timestamps. Missing timestamps remain null; malformed
timestamps mark diagnosis evidence invalid and policy investigates monitoring.
The expression timestamps are evaluation timestamps and do not establish the
age of the underlying samples used by `rate()` or `histogram_quantile()`.
Therefore the contract's aggregate Prometheus `observed_at` remains unknown,
and this metadata is not a freshness guarantee. The separate feature collector
retains its feature-vector timestamps and applies its configured age/skew
limits, but those rows are not inputs to `diagnose_service`. A future reliable
freshness boundary needs per-query source timestamps for underlying samples
(or an explicit freshness signal from the collector) plus an agreed policy
threshold; neither is currently available for this diagnosis path. The
canonical contract intentionally does not collapse the mixed query timestamps
into its single Prometheus `observed_at`; a future contract integration should
first represent result timestamps and their provenance separately from
observation timestamps.

**Phase F.6 vertical-slice audit:** The integrated response graph supports
classification, deterministic proposal/policy, durable approval, recheck,
execution-mode handling, verification, and audit references. The legacy
advisory workflow intentionally stops after advisory RCA and deterministic
decision policy; it does not request approval or execute/verify actions. The
integrated response now exposes additive `contract_status` and
`contract_error` fields, and blocks approval if contract validation fails.
Recovery contracts require both `execution.executed` and `execution.ok` to be
true. Verification also requires an explicit incident list; a missing or
malformed list is unavailable evidence, not proof of no active incidents.
Dry-run remains explicitly unexecuted with verification `not_run`; pre-
execution audit remains fail-closed. Verification freshness itself remains
limited by the observer contract and is not a substitute for the Prometheus
freshness limitations documented above.

**Phase F.7 RCA audit:** The Groq-backed RCA request is optional and bounded:
the client timeout defaults to 20 seconds (`KYBERNAUT_RCA_TIMEOUT_SECONDS`),
LangChain retries are disabled, and the adapter permits at most two total
attempts for connection, timeout, rate-limit, and server errors. Structured
output and input text are bounded; only selected diagnosis, telemetry,
Kubernetes symptom, and top-two runbook fields are sent, with a 12,000
character JSON ceiling and an explicit truncation flag. Incident and runbook
values are framed as untrusted evidence in the prompt. This is mitigation,
not a guarantee against prompt injection. RCA content is no longer added to
the audit event; only status and an error code are recorded. Both workflows'
deterministic policy is independent of RCA recommendations, targets, and
confidence. Invalid structured results are reported as RCA status `invalid`;
provider failures remain `unavailable`. The installed versions inspected were
`langchain-groq` 1.1.3, `langchain-core` 1.6.12, and `groq` 0.37.1; the
installed `ChatGroq` constructor exposes `timeout` and `max_retries`.

**Phase F.8 historical design:** Monitoring trigger alternatives and the
versioned evaluation design are documented in
[MONITORING_AND_EVALUATION_DESIGN.md](./MONITORING_AND_EVALUATION_DESIGN.md).
That phase found only request-driven triggers; Phase 4.18 later confirmed the
existing background monitor and dual-trigger design. No alert webhook is
implemented.

## 1. Current-state inventory

### Package and runtime

- `app/agents/` remains empty. `app/models/` contains the strict
  [incident contract](../app/models/incident_contract.py) added in Phase F.2,
  but no trained model artifacts. The existing operational implementation
  is under `app/services/`; there are already two LangGraph workflows and one
  LLM-backed RCA service.
- [app/main.py](../app/main.py) is the FastAPI entry point. Startup initializes
  SQLite and fits anomaly policies from the local telemetry baseline. Routes
  provide on-demand incident detection, diagnosis, decisions, Kubernetes
  observation, advisory workflow, and remediation. The authenticated
  operations router adds a response workflow, approval decisions, audit and
  timeline reads, and cost-report reads.
- Prometheus is queried at `http://localhost:9090` by existing services.
  The API also starts an application anomaly monitor in its lifespan by
  default, subject to explicit disable configuration. Operator-initiated
  workflows remain supported. No alert-webhook ingestion path is implemented.
- [requirements.txt](../requirements.txt) includes FastAPI/Pydantic,
  HTTPX, LangGraph, LangChain/Groq, scikit-learn, pytest, and supporting
  packages. There is no separate model-serving or model-registry dependency.

### Existing implementations

| Capability | Implementation and evidence | Current limitation |
| --- | --- | --- |
| Monitoring and feature collection | [prometheus_features.py](../app/services/prometheus_features.py) queries request rate, P95 latency, process memory, and CPU for four allowlisted services. It validates numeric values, sample age (default 30 seconds), and within-row timestamp skew (default 10 seconds). The lifespan monitor polls the existing anomaly evaluator and records operational status at `GET /monitor/status`. | Fresh instant-query timestamps do not guarantee fresh underlying scrapes. The status endpoint is authenticated and read-only; API liveness does not establish monitor health. No alert push is wired into the API. |
| Incident detection and persistence | [incident_detector.py](../app/services/incident_detector.py) reads Prometheus `up` and active target state, distinguishes service down from scrape/target problems, and persists incidents through [incident_repository.py](../app/services/incident_repository.py). | This legacy availability-detector path remains request-driven and does not itself emit the shared incident contract; the separate anomaly monitor is scheduled but does not schedule this detector. An empty availability result is inconclusive rather than down. |
| Kubernetes evidence | [k8s_observer.py](../app/services/k8s_observer.py) reads only Pods, Events, and Deployments in `kybernaut-lab`; recognizes container failures, recent OOM/Error terminations, failed Pods, deployment replica deficits, and recent Warning events. It retains recent Evicted warnings for deleted Pods and includes Pod UID/timestamps. | Lab- and resource-specific. It does not watch the Kubernetes API continuously. It uses `kubectl` subprocess calls with a per-call timeout and requires cluster credentials/context at runtime. |
| Deterministic diagnosis | [incident_diagnoser.py](../app/services/incident_diagnoser.py) queries availability, five-minute HTTP 5xx rate, and P95 latency, with a 10-second HTTP client timeout. Missing metrics are represented as unavailable evidence and route to monitoring investigation rather than a healthy/zero result. | Fixed thresholds and queries; Prometheus calls are sequential. Freshness is not established by this service. |
| Classification | [incident_classifier.py](../app/services/incident_classifier.py) maps Kubernetes evidence to `cpu_spike`, `memory_leak`, `pod_crash`, `disk_full`, `deployment_failure`, or `unknown`; telemetry-only candidates have confidence at most 0.5 and no inferred target. It rejects malformed timestamps and equal-time category conflicts. | The `diagnosis` input is intentionally excluded from benchmark category selection; service findings remain inputs to deterministic decision policy. CPU/memory labels from telemetry are candidates, not confirmed incidents; they cannot target a workload for action. |
| Canonical incident contract | [incident_contract.py](../app/models/incident_contract.py) validates evidence source states, timestamps, classification confidence/severity, and Kubernetes target backing. [incident_response_graph.py](../app/services/incident_response_graph.py) and the older [incident_workflow.py](../app/services/incident_workflow.py) expose the same contract alongside their existing response fields. | The public routes retain legacy fields and add the contract. The advisory workflow's legacy diagnosis payload may omit evidence-completeness metadata; the contract records such Prometheus evidence as unavailable rather than inferring healthy status. |
| Anomaly logic | [anomaly_policy.py](../app/services/anomaly_policy.py) fits per-feature median/MAD statistics, uses standard-deviation fallback for near-zero MAD, and labels only high deviations in selected features as actionable candidates. [anomaly_runtime.py](../app/services/anomaly_runtime.py) loads `data/telemetry_baseline_v6.csv` and fits one policy per service during application startup. | Startup fitting is in-memory, with no serialized model, registry, validation report, or drift lifecycle. Missing/stale telemetry returns unavailable, not normal. |
| Alternative anomaly model | [anomaly_detector.py](../app/services/anomaly_detector.py) implements an Isolation Forest with a fixed four-feature schema, minimum 20 training rows, deterministic random state, and score/flag output. | It is not used by `anomaly_runtime.py`; it is a standalone scoring component, not the active production path. |
| Root-cause assistance | [rca_agent.py](../app/services/rca_agent.py) calls a configured Groq-hosted chat model, requests a Pydantic-validated advisory `RCAResult`, and requires a configured API key. [runbook_retriever.py](../app/services/runbook_retriever.py) provides deterministic weighted keyword retrieval from local Markdown runbooks. | Each provider call has a configurable timeout (20 seconds by default) and the code retries selected transient errors once after a 0.25-second delay. This is a source-level bound, not measured end-to-end latency or provider reliability. RCA is optional and failure-tolerant in the graph; it must not be an authority for policy or tool execution. |
| Decision and policy | [decision_engine.py](../app/services/decision_engine.py) and the older advisory graph choose investigation actions. [policy_engine.py](../app/services/policy_engine.py) validates a strict `ProposedAction`, allowlists actions, namespace/deployments, confidence, rate limits, scale bounds, and restart blast radius; missing action-count or deployment evidence fails closed. | The advisory graph routes invalid, unavailable, or stale source evidence to monitoring investigation unless a higher-priority confirmed finding selects a more specific response. This is implemented behavior, not approval of the still-pending D-FRESH-03 policy in [MONITORING_TRIGGER_DECISION.md](./MONITORING_TRIGGER_DECISION.md). Two decision paths coexist. Policy configuration is environment-based. The default is no automatic execution, but safe deployment depends on preserving that default and verifying configuration. |
| Response orchestration | [incident_workflow.py](../app/services/incident_workflow.py) implements an advisory graph. [incident_response_graph.py](../app/services/incident_response_graph.py) integrates evidence collection, classification, RCA, action planning, policy, approval, remediation, verification, and audit. | Duplicate entry points and service-versus-Kubernetes branches remain. A few stages are best-effort and some workflow state is reconstructed from SQLite rather than checkpointed by LangGraph. |
| Remediation | [k8s_remediation.py](../app/services/k8s_remediation.py) builds fixed argv vectors for restart, scale, and rollback; defaults to dry-run and refuses live execution unless the current context is `kind-kybernaut`. [remediation_service.py](../app/services/remediation_service.py) is a separate allowlisted service-restart path with outage/health checks, attempt reservation, and audit. | The two remediation interfaces differ. The integrated graph plans actions only for selected classifications; `disk_full` has no automated proposal. Live actions are not verified by a unit test against a cluster, nor should they be in ordinary CI. |
| Verification | [verification.py](../app/services/verification.py) re-observes a deployment, requires no related active incident and sufficient available replicas, and needs consecutive healthy samples. | Verification runs only after an action reports `executed`; a dry run correctly reports not executed and not recovered. The legacy service restart has a simpler health-endpoint verification. |
| Audit and approvals | [audit_repository.py](../app/services/audit_repository.py) stores event and approval rows in SQLite, atomically expires/decides pending approvals, and limits query sizes. The response graph requires a pre-execution audit write before a tool call. | Most workflow audit events are best-effort; only the pre-execution event fails closed. SQLite audit storage is not tamper-evident and has no documented retention/export policy. |
| Optimization | [cost_optimizer.py](../app/services/cost_optimizer.py) produces right-sizing candidates from supplied utilization and pricing; it requires at least 24 samples per metric and marks savings as potential. `scripts/generate_cost_report.py` writes a report; the API reads the latest report. | Offline batch/report flow only. No utilization ingestion, scheduler, approval workflow, or infrastructure change execution is integrated. |

### Existing tests and evaluation

- Tests cover telemetry schema/freshness, anomaly policy and Isolation Forest
  behavior, classification, graph approvals/dry-run/retry/audit paths, policy
  allowlists, remediation guardrails, verification, and API access controls.
  The end-to-end telemetry integration test uses synthetic inputs and a
  temporary SQLite database.
- `benchmark/scenarios.json` describes 20 expected cases (four per category).
  [docs/EVALUATION.md](./EVALUATION.md) states that this matrix is not a
  completed live benchmark and that no actual 20-case metrics report is
  established by synthetic tests.
- `scripts/evaluate_anomaly_detector.py` and
  `scripts/evaluate_hybrid_detector.py` use chronological held-out baseline
  telemetry without incident labels and synthetic probes. Their alert rates
  are exploratory, not precision/recall or production accuracy.
- Existing baseline files are CSV telemetry datasets under `data/`. No
  serialized trained model or artifact is present in `app/models/`, `models/`,
  or `artifacts/`. The runtime refits the MAD policy at startup; there is no
  persisted trained artifact to version.

## 2. Existing versus missing functionality

| Stage | Status | Grounded assessment |
| --- | --- | --- |
| Monitor | **Partial** | Default-enabled background anomaly polling and operator-initiated workflows both exist. The status endpoint reports available process state; alert ingestion and broader source coverage remain absent. |
| Detect | **Partial** | Availability, target-health, Kubernetes evidence, and anomaly candidates exist, but there is no single trigger and incident envelope joining every source. |
| Diagnose | **Partial** | Rule-based diagnosis, evidence-based Kubernetes classification, runbook retrieval, and optional LLM RCA exist. The deterministic classifier currently does not use its diagnosis argument. |
| Decide | **Implemented with overlap** | Deterministic policy and strict action schema exist, but an older decision engine/advisory graph and the integrated response graph create multiple paths to maintain. |
| Remediate | **Partial, gated** | Allowlisted service and Kubernetes tools exist; dry-run/default and approval gates exist. Actions are narrow, and several incident categories have no proposal. No general autonomous agent/tool loop exists. |
| Verify | **Partial** | Fresh Kubernetes verification requires complete, well-formed incident evidence and consecutive healthy observations; invalid verification budgets fail explicitly. Legacy restart checks `/health`. Verification is not meaningful after dry-run and is intentionally skipped. |
| Audit | **Partial** | SQLite incidents, approvals, attempts, and audit events exist. Mandatory pre-execution audit is fail-closed, but other workflow events are best-effort; audit retention/integrity guarantees are absent. |
| Optimize | **Partial** | Offline, sample-gated cost recommendations exist; collection, scheduled execution, approval, and applied-savings feedback are not integrated. |
| Agent package | **Missing as a package** | `app/agents/` is empty, but LangGraph and RCA responsibilities already exist in services. Adding a parallel agent per stage would duplicate current code. |
| Model package/lifecycle | **Partial** | The active statistical policy and an unused Isolation Forest scorer exist in services. There is no model artifact, registry, promotion, rollback, or labeled evaluation result. |

## 3. Proposed agent architecture and responsibilities

### Design rule

Use **one bounded workflow orchestrator with deterministic service nodes**,
not a set of autonomous agents that can invent tools or call one another.
Populate `app/agents/` only for thin LangGraph node/adaptor code if the
workflow needs a clear package boundary. Keep Prometheus/Kubernetes I/O,
classification, policy, remediation, verification, audit, and cost logic in
their existing service modules. Do not copy those rules into LLM prompts.

The existing `incident_response_graph.py` is the closest foundation. Its
state should become an explicit, versioned contract (event/incident ID,
service and target, evidence with source and timestamps, status per source,
classification/confidence, proposal, policy result, approval, execution,
verification, and audit correlation). Existing dict payloads and the current
`TypedDict` are not yet one shared validated contract.

### Stage responsibilities and contracts

| Stage | Inputs → outputs / state transition | Responsibility and permission boundary | Failure, timeouts, retries, and audit |
| --- | --- | --- | --- |
| **Monitor** | Prometheus samples or a future alert event → timestamped observation envelope. | External Prometheus remains the metrics collector. A future adapter may accept only allowlisted service/metric labels; it does not get remediation credentials. Do not add a polling agent until cadence, ownership, and alert source are chosen. | Mark scrape/feature status explicitly; stale, missing, or malformed samples are `unavailable`, never zero/healthy. Current HTTP calls use 10-second client timeouts; add a documented total workflow budget before adding concurrency. Audit source, sample timestamps, and correlation ID without copying secrets or unbounded payloads. |
| **Detect** | Observation envelope + Kubernetes snapshot → candidate incident or `unknown`, with evidence references and fingerprint. | Reuse `incident_detector.py`, `k8s_observer.py`, `prometheus_features.py`, and `incident_repository.py`. Keep observer read-only and restricted to configured namespaces/resources. No model or LLM may create an unsupported fact. | Preserve each source's unavailable state; do not resolve incidents after failed observation. Bound Kubernetes calls and avoid blocking the event loop. Test no-data, stale data, deleted-Pod historical events, duplicate fingerprints, and recovery/resolution. |
| **Diagnose** | Incident and evidence bundle → findings, supported hypotheses, confidence, and missing evidence. | Reuse deterministic diagnosis/classification and optional `runbook_retriever.py` + `rca_agent.py`. Treat retrieved runbooks as guidance, not evidence. LLM receives the minimum redacted evidence needed; no tools or credentials. | Prometheus calls currently have per-client 10-second timeout; Kubernetes subprocess calls have a per-command 20-second timeout. Define an end-to-end deadline, bounded retry policy (no retries for non-idempotent actions), and provider timeout before productionizing. Keep deterministic diagnosis when RCA fails; record `rca_status=unavailable`. |
| **Decide** | Classified incident + evidence + policy config/action history → allow/deny, approval requirement, or investigation-only decision. | Reuse `ProposedAction` and pure `evaluate_policy`. Only fixed action enums and allowlisted namespace/deployment/resource names can pass. LLM output must never bypass schema/policy. Default is approval required; automatic execution remains off unless deliberately configured and separately approved. | Missing deployment/action-history state denies. Keep policy decisions and rule results in audit. Test all boundary values, stale proposals, replay, rate limits, and changed evidence between approval and execution. |
| **Remediate** | Approved, fresh, policy-allowed proposal → dry-run/execution result. | Call only fixed `k8s_remediation.execute_action` or a separately selected legacy service helper; never expose shell, arbitrary kubectl, or generic tool invocation. Keep `dry_run` default, exact lab context guard, namespace and action allowlists, JWT permission, and persisted human-approval controls. | Audit precondition must succeed before execution; command timeout is 60 seconds for kubectl and legacy helper wait is 30 seconds. Fail closed on tool/context/audit errors. Retry only through the bounded graph path with fresh evidence and a new approval. |
| **Verify** | Executed action + target → recovered/not-recovered/unknown with checks. | Reuse `verify_recovery` and existing fresh Kubernetes observation. Recovery is a separate result from command success. Do not mark dry-run as verified. | Current verifier defaults to five attempts, 3 seconds apart, and two consecutive healthy observations. Observation failure counts as failed evidence. Bound total duration, require target identity, and test eventual consistency and unavailable observations. |
| **Audit** | Every stage event → correlated, timestamped event and approval state. | Reuse `audit_repository.py`; record actor, source/evidence references, policy, approval, action mode/result, verification, and correlation ID. Avoid logging secrets, full environment, or unnecessary raw payloads. | Preserve mandatory pre-execution fail-closed write. Decide which events require durable writes; surface DB errors. Define retention, export, access, integrity, and backup requirements before claiming compliance. |
| **Optimize** | Sufficient utilization samples + operator-provided pricing catalog → potential savings report. | Keep `cost_optimizer.py` an offline recommendation service. It must not resize resources or change policy. Treat catalog/example pricing as untrusted input that requires provenance and validation. | Current minimum is 24 samples per CPU/memory metric. Report missing/stale sample periods and potential vs. realized savings separately. Future application requires a distinct approval and change-control workflow. |

### Proposed dependency and integration map

```text
Prometheus / future alert adapter ─┐
                                  ├→ validated evidence envelope
read-only Kubernetes observer ───┘
       → incident repository / fingerprint
       → deterministic classifier and diagnosis
       → optional runbook retrieval + advisory RCA
       → ProposedAction schema + deterministic policy
       → approval record and fresh policy/evidence recheck
       → allowlisted dry-run/live tool
       → fresh verification
       → correlated audit timeline

Utilization input + priced catalog → offline cost report
```

Do not add another `MonitorAgent`, `DiagnosisAgent`, `PolicyAgent`, or
`RemediationAgent` that repeats existing services. Any future `app/agents/`
module should only adapt validated state to graph nodes and own orchestration;
it must not own independent copies of business policy.

## 4. Proposed model architecture and lifecycle

### Current schema, source, and behavior

- Feature order is fixed in
  [anomaly_detector.py](../app/services/anomaly_detector.py):
  `request_rate`, `p95_latency_seconds`, `memory_bytes`, and
  `cpu_cores_used`. Units are respectively requests/second, seconds, bytes,
  and CPU cores. Inputs must be finite, numeric, non-negative rows.
- [prometheus_features.py](../app/services/prometheus_features.py) derives
  these from Prometheus queries. It omits incomplete, stale, or excessively
  skewed service rows rather than substituting zeros.
- Training-data collection is implemented by
  [collect_ml_baseline_v6.py](../scripts/collect_ml_baseline_v6.py): 60
  samples, 30 seconds apart, for available services, with an exclusive
  output-file check. The runtime reads `data/telemetry_baseline_v6.csv`.
- The live `anomaly_runtime` fits `DirectionAwareAnomalyPolicy` in memory per
  service at startup and requires at least 20 rows per service. The active
  output is a feature-level direction/deviation classification, not a
  persisted prediction model.
- `TelemetryAnomalyDetector` uses scikit-learn `IsolationForest` with 200
  estimators, contamination `0.05`, and random state `42` by default, but
  runtime integration does not select it.
- There are no serialized model files, artifact metadata, model version
  loader, persisted training pipeline, feature drift monitor, or rollback
  mechanism. CSV baselines are data inputs, not model artifacts.
- No trustworthy labeled model evaluation metrics are documented. The
  baseline holdout is unlabelled; synthetic probes demonstrate only
  expected behavior for those constructed inputs. The separate benchmark
  requires actual recorded cases and healthy controls.

### Recommendation

**Do not train or add a new model in the initial implementation slice.**
First integrate and measure the existing deterministic path. The direction-
aware MAD policy is already the active anomaly evaluator; the Isolation Forest
should remain experimental until a labeled comparison demonstrates a
specific benefit. Do not combine scores or tune thresholds based only on
unlabelled alert rates.

If later evidence justifies a learned model, use an explicit lifecycle:

1. Version the feature schema, units, service identity, source query
   definitions, sample freshness limits, training window, data checksum,
   code/dependency versions, parameters, and validation results together.
2. Split chronologically and by service/time window to avoid leakage; keep
   labeled injected incidents and healthy controls separate from training.
3. Compare the current directional MAD policy and any candidate model on the
   same held-out labeled cases. Report detection precision/recall/F1, per-
   category classification performance, false positives per healthy-control
   window, latency, missing-data behavior, and calibration. Do not invent
   acceptance targets; obtain explicit operational thresholds before
   promotion.
4. Load only a validated, versioned artifact with a compatible feature
   schema. Reject incompatible/corrupt artifacts at startup; expose model
   version and health without exposing data. Keep deterministic fallback and
   rollback to the last accepted artifact.
5. Promote in shadow/read-only mode first. The model can propose an anomaly
   candidate, never a target or action. Deterministic classifier, policy,
   human approval, allowlists, and audit remain authoritative.

`app/models/` should not become a dumping ground for pickles. If it is
populated later, define typed feature/artifact metadata and a narrow loader
with trusted artifact provenance; keep large/versioned artifacts outside the
source tree unless repository policy explicitly approves them.

## 5. Test strategy and measurable acceptance criteria

Use the existing pytest style: deterministic fixtures, fake Prometheus and
Kubernetes observations, temporary SQLite databases, and mocked remediation
and LLM boundaries. Ordinary CI must never contact a live cluster, Prometheus,
Groq, or execute remediation.

1. **Contracts and adapters:** For each evidence source, validate required
   fields, feature names/units, timestamps, resource identity, bounds, and
   unavailable status. Pass criterion: malformed, stale, future, partial, or
   wrong-service inputs never become a healthy value or actionable target.
2. **Detection/classification:** Test all five benchmark categories and
   `unknown`, evidence precedence, historical event lookback/UID behavior,
   diagnosis integration, and no target for telemetry-only inference. Pass
   criterion: deterministic fixture inputs produce their expected category,
   target only when evidence establishes it, and traceable evidence/source
   timestamps.
3. **Workflow vertical slice:** Run a synthetic Kubernetes pod failure
   through evidence collection → classification → proposal → policy →
   pending approval → approval → dry-run → audit. Pass criterion: no tool
   execution before approval; dry-run is explicitly not executed/not
   recovered; audit contains correlation and each expected transition.
4. **Policy/remediation boundary:** Test denied namespace/resource/action,
   missing/stale state, rate limit, expired/replayed approval, wrong context,
   command timeout/failure, and audit-store failure. Pass criterion: every
   unsafe or ambiguous case fails closed, and no unapproved tool invocation
   occurs.
5. **Verification and recovery:** Test healthy consecutive samples, a
   transient unhealthy sample resetting the consecutive count, unavailable
   observer, missing deployment, and bounded attempts. Pass criterion: only
   required consecutive fresh healthy observations resolve an executed
   action; unavailable evidence never counts as recovery.
6. **LLM and retrieval:** Mock valid/invalid structured output, provider
   timeout/unavailable, prompt injection in evidence/runbook text, and
   excessive input size. Pass criterion: RCA is advisory; invalid or
   unavailable output cannot alter deterministic policy, invoke tools, or
   claim unsupported evidence.
7. **Model evaluation:** Keep current detector unit tests. For promotion,
   require a versioned, labeled, chronological holdout and all five incident
   categories plus healthy controls; publish per-category metrics and
   confidence intervals or sample-count caveats. Pass criterion: no artifact
   is promoted without a review-approved threshold, reproducible data/code
   provenance, schema compatibility, and a tested fallback/rollback.
8. **Integration and live acceptance:** Unit/contract/integration tests
   should run entirely from synthetic fixtures. The existing
   `docs/EVALUATION.md` process governs any separately authorized lab
   benchmark; do not call fixture success a live detection result.

## 6. Dependency map

```text
Prometheus feature schema + timestamps ─┐
Kubernetes observer snapshot + UID ─────┼→ evidence contract
                                        ├→ detection/classification
incident repository/fingerprints ───────┘
      → deterministic diagnosis
      → optional local runbook retrieval / external advisory RCA
      → policy config + action history
      → durable approval + recheck
      → scoped remediation adapter
      → verification
      → audit event/timeline

separate future path:
versioned labeled dataset → evaluation → reviewed model artifact
                         → shadow inference → promotion/fallback

utilization + pricing catalog → cost recommendation report only
```

The service graph is intentionally read-only until policy gates pass.
Credentials belong only at the adapter that needs them: Prometheus read
access, Kubernetes read access for observation, narrowly scoped write access
for the approved remediation tool, and provider credentials only for optional
RCA. Never place credentials in graph state, model inputs, audit details, or
LLM prompts.

## 7. Implementation phases

1. **Contract and path consolidation (lowest risk):** agree on the event and
   evidence contract, service identity, correlation/fingerprint rules, and
   supported trigger model. Document which API/graph is canonical. Do not add
   new agents or models.
2. **Vertical slice hardening (low risk):** extend synthetic tests around the
   existing Kubernetes failure → classifier → proposal → policy → approval
   → dry-run → audit path. Align status/error schemas and ensure diagnosis
   findings are deliberately incorporated or explicitly excluded from
   classification. Keep live execution disabled.
3. **Evidence lifecycle and monitoring trigger (medium risk):** Phase 4.18
   confirmed the existing default-enabled monitor alongside operator API
   workflows and added a read-only status surface. Future work should focus on
   evidence freshness, timeout, correlation, and source-health behavior; do not
   add duplicate pollers or Prometheus collectors.
4. **Decision and operational safety (medium/high risk):** unify older
   decision and remediation routes with the canonical policy boundary; make
   authorization, stale-proposal recheck, fail-closed audit, attempt limits,
   and explicit dry-run response consistent. Require isolated tests before
   any separately authorized live use.
5. **Advisory RCA hardening (medium risk):** define provider timeout/retry
   budget, data minimization/redaction, prompt-injection tests, model/provider
   version recording, and structured-output failure behavior. Keep it
   optional and non-authoritative.
6. **Model decision (low risk until deployment):** first evaluate existing
   MAD and Isolation Forest against labeled records. Implement artifact
   storage/loading only if it improves agreed metrics and adds operational
   value; shadow first, with rollback.
7. **Optimization integration (later):** agree on trustworthy utilization
   source, pricing provenance, cadence, approval, and savings verification.
   Retain recommendation-only behavior until explicitly approved.

## 8. Open questions and assumptions

- The current trigger design deliberately combines the background monitor and
  operator-initiated APIs. Detection latency objectives and any alert-webhook
  ownership remain undecided.
- Which clusters/namespaces and deployment names are supported beyond the
  current `kybernaut-lab` scope? What Kubernetes read/write identities and
  least-privilege RBAC are acceptable?
- Should the older service-restart route remain supported, or should it
  delegate to the canonical graph? How should incompatible audit and
  incident records be migrated without losing history?
- Which audit event failures must block progress, what retention/export
  period applies, and is tamper evidence or external append-only storage
  required?
- Is sending incident evidence and runbook text to the configured external
  Groq provider permitted? What redaction, retention, residency, timeout, and
  provider-version requirements apply?
- What incident labels and healthy controls can be collected safely, and
  who reviews the accepted precision/recall, false-positive, latency, and
  recovery thresholds?
- Is the current per-service MAD policy sufficient? If not, what measurable
  shortcoming must a learned model solve? Who owns baseline refresh, drift
  review, artifact signing/storage, promotion, and rollback?
- Which utilization source and authoritative pricing catalog should power
  optimization, and who approves any change based on a potential savings
  report?

Until these decisions and measurable acceptance thresholds are approved,
keep new workflow nodes advisory, new model proposals read-only, and all
remediation approval-gated with dry-run as the default.
