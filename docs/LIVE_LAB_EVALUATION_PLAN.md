# Controlled Live-Lab Evaluation Plan

**Status: planning only — BLOCKED.** This document is not authorization to
run a scenario. No live or synthetic benchmark scenario is authorized by this
plan. Any future execution requires a separately reviewed and recorded
operator authorization that satisfies every mandatory gate below.

This plan is based on the canonical
[scenario matrix](../benchmark/scenarios.json), the existing
[failure simulator](../app/services/failure_scenarios.py), the
[Kubernetes observer](../app/services/k8s_observer.py), the
[incident classifier](../app/services/incident_classifier.py), and the
[result contract](./EVALUATION.md). It does not add run-time behavior or
assume that successful injection proves detection.

## 1. Authorization and environment preflight

Complete and review every field before scheduling a future live run. An
unanswered, ambiguous, or unverified item blocks the run.

| Gate | Required operator decision or evidence | Status |
| --- | --- | --- |
| Environment identity | Record the exact Kubernetes context, cluster identity, namespace, owner, and how ownership was verified. The simulator is fixed to context `kind-kybernaut` and namespace `kybernaut-lab`; confirm that these still identify the explicitly approved lab. | Required; unfilled |
| Isolation and resources | Identify allowed workloads, nodes, namespaces, telemetry systems, and resource boundaries. Confirm the lab is isolated from production and unrelated workloads, and that the approved scenario cannot affect them. | Required; unfilled |
| Scenario authorization | Approve each scenario ID separately, including its expected impact, variant preconditions, resource bounds, and whether the scenario remains blocked. Approval of one scenario does not approve another or a retry. | Required; see scenario table |
| Authorized identities | Name the identities permitted to schedule/start, approve any action, stop, perform cleanup, and independently review the evidence. Define separation of duties where required. | Required; unfilled |
| Production exclusion | Explicitly confirm production resources, credentials, databases, services, port 8000, and unrelated workloads are out of scope and cannot be selected by the procedure. | Required; unfilled |
| Baseline health | Define and capture operator-approved healthy-state evidence for the lab, node readiness/pressure, workloads, and `demo-web` before each scenario. Define acceptable limits; this plan sets no numerical thresholds. | Required; unfilled |
| Observability | Verify the specific Kubernetes, Prometheus/Metrics API, and other sources required for the chosen scenario are available and their timestamps and identity can be recorded. Decide what failure of a source blocks or invalidates. | Required; unfilled |
| Stop authority and escalation | Name the person with unambiguous stop authority and the escalation contact/channel. Confirm they can be reached during the run. | Required; unfilled |
| Test window | Approve start/end times, timezone, maximum duration, and a response if the window expires. | Required; unfilled |
| Approval and evidence retention | Identify required approval records, evidence custodian, access-controlled retention location, retention period, and permitted redaction. No location or retention period is selected here. | Required; unfilled |
| Remediation boundary | Keep remediation in dry-run mode. Decide whether any separately approved live remediation is in scope; this plan does not grant that authorization. If none is approved, execution of remediation is prohibited. | Dry-run boundary required; live actions unauthorized |
| Cleanup and rollback | Review the existing simulator cleanup behavior, assign the cleanup operator, define baseline-restoration checks, and approve stop/rollback actions before injection. Do not assume cleanup is automatic or guaranteed. | Required; unfilled |
| Explicit authorization | Record a final approval that names the environment, window, scenario IDs, authorized identities, stop authority, evidence retention, and approved cleanup/rollback procedure. | Not granted by this planning phase |

### Global execution boundaries

- Use only the explicitly approved lab context and namespace. Before every
  failure injection, independently verify the current Kubernetes context is
  exactly `kind-kybernaut`; also verify node readiness and pressure and inspect
  the lab workloads. The simulator pins its kubectl calls to that context and
  namespace, but that code guard does not replace operator preflight.
- Use only the existing failure simulator and its supported cleanup. Do not
  substitute ad hoc kubectl commands, apply manifests, install dependencies,
  change configuration, or modify application code as part of the run.
- Run at most one scenario at a time. Capture the baseline and evidence,
  observe/classify while evidence remains available, and clean up immediately
  before considering another scenario.
- The simulator's default is dry-run. An actual injection requires its
  explicit execution mode and the separate authorization above. A dry-run is
  not a live scenario result. The response graph's remediation must remain
  dry-run; do not execute automatic remediation against live workloads.
- Before invoking any response workflow, verify the effective runtime
  remediation mode is dry-run and automatic remediation is disabled, without
  exposing secrets or reading `.env` values into retained evidence. If either
  setting cannot be verified, do not invoke the response workflow. Do not
  submit an approval decision or resume an approval during this evaluation.
  The separately approved scenario injection is the only allowed
  state-changing action against lab workloads.
- Stop rather than continue or retry if the scenario is not explicitly
  approved, preflight fails, evidence is unavailable, cleanup fails, node
  pressure appears, or an out-of-scope resource may be affected.

## 2. Scenario-by-scenario plan

The matrix has 20 positive cases, four per category. Its contextual variants
are labels only: the simulator does not enforce traffic or replica-count
conditions. Verify and record those conditions independently. Every row
requires its own approval and a fresh preflight. No row is currently approved
for execution.

| Scenario ID / expected category | Matrix variant and implementation-supported preconditions | Exercise evidence and expected observation | State change, authorization, verification, cleanup, and current gate |
| --- | --- | --- | --- |
| `cpu-spike-1` / `cpu_spike` | Baseline 300m CPU burn. Simulator prerequisite: simulator workloads and Kind cluster exist. Confirm workload/resource limits and required CPU telemetry before approval. | Preserve simulator result and timestamps; observe CPU usage/throttling evidence from the approved source and run observer/classifier while evidence is present. Expected category is a test target, not a guaranteed detection. | Scales `sim-cpu-stress` to one; supported cleanup scales it to zero. Approve resource impact, verify cleanup and lab baseline. **Blocked** until CPU telemetry and resource safeguards are confirmed. |
| `cpu-spike-2` / `cpu_spike` | Repeat after cleanup and 10 minutes idle. Verify idle interval and no active simulator workload. | Same CPU evidence as `cpu-spike-1`, with separately retained interval and run identity. | Same scale-up/scale-down state changes; cleanup and baseline verification before repeat. **Blocked**; repeat requires distinct approval. |
| `cpu-spike-3` / `cpu_spike` | Run with two healthy `demo-web` replicas. Verify the replica count and readiness; simulator does not enforce this variant. | CPU usage/throttling evidence plus independently recorded `demo-web` replica/readiness state. | Scales the CPU simulator deployment; clean it to zero and confirm `demo-web` remains healthy. **Blocked** until the variant and resource safeguards are approved. |
| `cpu-spike-4` / `cpu_spike` | Separately confirmed low-traffic window. Define and capture the independent evidence used to establish low traffic; simulator does not enforce it. | CPU evidence plus the approved traffic-window evidence and timestamps. | Scales the CPU simulator deployment; clean it to zero and verify baseline. **Blocked** until a low-traffic criterion and resource safeguards are approved. |
| `memory-leak-1` / `memory_leak` | ~1 MiB/s growth to configured 96 Mi limit, with OOMKilled restarts. Simulator prerequisite: simulator workloads exist. Confirm memory limits, node headroom, and required telemetry. | Preserve memory trend and Pod status/events including termination reason, restart count, Pod UID, and timestamps. Observer should capture available failure evidence; classifier output is recorded separately. | Scales `sim-memory-leak` to one and may cause OOM/restarts; cleanup scales it to zero. Approve impact and stop bounds; verify no simulator workload remains and baseline is restored. **Blocked** until memory/pressure safeguards are confirmed. |
| `memory-leak-2` / `memory_leak` | Repeat after simulator cleanup. Confirm cleanup and healthy baseline before repeat. | Same as `memory-leak-1`, with distinct run/case evidence. | Same state changes and cleanup. **Blocked**; separate approval required. |
| `memory-leak-3` / `memory_leak` | Run with two healthy `demo-web` replicas; verify actual ready replicas because the simulator does not enforce the variant. | Memory trend and termination evidence plus replica/readiness snapshots. | Same state changes; verify `demo-web` remains healthy and simulator is zero after cleanup. **Blocked** until safeguards and variant are approved. |
| `memory-leak-4` / `memory_leak` | Repeat only after a failed attempt has been cleaned up. Require review of the prior attempt, cause, and cleanup evidence before any new approval. | Preserve prior failed-attempt reference separately; new run must have its own timestamps, memory/Pod evidence, and classification. | Same state changes. This is not an automatic retry. **Blocked** until a reviewer accepts prior cleanup and separately authorizes the repeat. |
| `pod-crash-1` / `pod_crash` | Signal PID 1 in the first Ready `demo-web` Pod returned by Kubernetes. Simulator requires at least two Ready replicas, exactly one declared Ready container on the selected Pod, and confirms PID 1 is `nginx` before signaling. | Record selected Pod name, UID and container, baseline restart/termination state, simulator output, new non-zero termination or increased restart evidence, and timestamps. Observer/classifier must be run while evidence is available. | Sends `kill -9 1`; Kubernetes is expected to restart the container; simulator has no cleanup mutation. Verify recovery and both replicas healthy. **Blocked**; prior live pod-crash authorization is exhausted, so new explicit approval and review are mandatory. |
| `pod-crash-2` / `pod_crash` | Repeat after recovery; simulator again selects first Ready Pod. Verify recovery and at least two Ready replicas before approval. | Same UID-bound baseline/delta evidence; identify the actual selected Pod rather than assuming it is the same Pod. | Self-healing, no simulator cleanup command mutation. Verify baseline. **Blocked**; distinct approval and recovery review required. |
| `pod-crash-3` / `pod_crash` | Separately confirmed low-traffic window and at least two Ready replicas. Traffic condition is not simulator-enforced. | UID-bound termination/restart evidence plus independent traffic evidence. | Sends the signal to one selected container. Verify recovery and baseline. **Blocked** until low-traffic evidence and new approval are confirmed. |
| `pod-crash-4` / `pod_crash` | Independent repeat after recovery; confirm fresh healthy baseline and simulator preconditions. | New independent UID-bound evidence; do not reuse prior detection as proof. | Self-healing; no simulator cleanup mutation. Verify baseline. **Blocked**; a retry/repeat is separately authorized. |
| `disk-full-1` / `disk_full` | Writes 5 MiB every 2 seconds into a 40 Mi `emptyDir` until eviction. Simulator prerequisite: simulator workloads exist. Confirm node/storage headroom, exact resource bounds, and ability to stop safely before approval. | Capture Pod UID, `Evicted` status or Warning event, reason/message including ephemeral storage or `emptyDir`, event source timestamps, observer output, and classifier output. Capture promptly; evidence may outlive the Pod only through retained recent events. | Scales `sim-disk-fill` to one and intentionally causes eviction. Supported cleanup scales it to zero. **Blocked** until storage/node safeguards, event-retention observability, and cleanup behavior are reviewed and approved. |
| `disk-full-2` / `disk_full` | Repeat only after simulator cleanup and confirmation of healthy node/storage baseline. | Same as `disk-full-1`; retain unique event/Pod UID and timestamps for the new case. | Same state changes. **Blocked**; separate approval after baseline restoration. |
| `disk-full-3` / `disk_full` | Run with two healthy `demo-web` replicas; independently verify this contextual variant. | Eviction evidence plus `demo-web` replica/readiness baseline and post-run status. | Causes the simulator Pod to consume bounded `emptyDir` and evict; clean to zero and confirm unrelated lab workloads remain healthy. **Blocked** until storage safeguards are confirmed. |
| `disk-full-4` / `disk_full` | Separately confirmed low-traffic window; define evidence for traffic condition. | Eviction evidence plus the independent traffic-window record. | Same state changes and cleanup. **Blocked** until traffic criteria and storage safeguards are approved. |
| `deployment-failure-1` / `deployment_failure` | Changes `demo-web` image to the configured intentionally missing tag. Simulator prerequisite: current image is `nginx:1.28-alpine`. Verify exact current image, deployment ownership, desired/available replicas, and restoration image before approval. | Capture old/new image, rollout/deployment status, Pod UID/status/events showing `ErrImagePull` or `ImagePullBackOff`, unavailable replicas, observer and classifier results, and timestamps. | Changes the `demo-web` Deployment and can reduce availability. Supported cleanup restores `nginx:1.28-alpine` and waits for rollout status (120s command timeout). **Blocked** until service impact, restoration, rollback authority, and verification are explicitly approved. |
| `deployment-failure-2` / `deployment_failure` | Repeat only after the healthy image has been restored and verified. | New image transition, Pod/event evidence, and independent run identity; preserve the prior restoration evidence. | Changes `demo-web` image; supported cleanup restores the healthy image and checks rollout. **Blocked** until baseline restoration and separate approval. |
| `deployment-failure-3` / `deployment_failure` | Run with two `demo-web` replicas configured. Verify replica count and readiness before approval; simulator does not enforce this variant. | Image-pull and replica availability evidence plus the verified replica configuration. | Image change can affect the deployment. Restore the known healthy image, check rollout, and verify baseline. **Blocked** until the two-replica variant and impact are approved. |
| `deployment-failure-4` / `deployment_failure` | Separately confirmed low-traffic window; the simulator does not enforce traffic conditions. | Image-pull and availability evidence plus independently captured traffic-window record. | Image change can affect `demo-web`; restore and verify rollout. **Blocked** until low-traffic criterion, restoration, and impact are approved. |

### Per-category failure criteria

For every case, failure to capture the required evidence, inability to bind it
to the approved context/namespace/resource and run window, unavailable
observer/classifier output, unexpected impact, or unsuccessful cleanup makes
the case incomplete or failed; do not substitute expected labels for actual
observations. In addition:

- **CPU:** no approved-source CPU/throttling evidence, or telemetry unavailable
  during the approved observation window, means the exercise/detection result
  is not established.
- **Memory:** no attributable memory growth or expected OOM/restart evidence
  means the expected exercise is not established.
- **Pod crash:** no fresh, non-zero termination/restart attributable to the
  selected Pod UID means the simulator has not demonstrated the expected
  exercise. The simulator itself refuses to report success without this
  evidence.
- **Disk:** no attributable eviction evidence identifying ephemeral storage,
  disk/storage, or the constrained `emptyDir` means the expected exercise is
  not established. A pod deletion without retained relevant evidence is not
  detection.
- **Deployment failure:** no attributable `ErrImagePull`/`ImagePullBackOff`
  and deployment availability evidence means the expected exercise is not
  established. Failure to restore the healthy image and verify rollout is a
  stop condition.

### Healthy controls

The matrix also recommends five 15-minute controls: baseline without an
active simulator workload; healthy services after CPU, memory, and disk
cleanup; and healthy services after deployment-image restoration. Each
control requires its own approved window, start/end timestamps, evidence of
the healthy condition, observed alert/category, and failure reason. The
matrix's expected no-alert/`unknown` labels are evaluation targets, not
observed outcomes. Do not start a control if its required baseline has not
been restored and verified.

## 3. Evidence collection and review protocol

### Run and scenario identity

- Preserve the exact scenario-matrix bytes used for metric calculation and
  record the calculator's SHA-256 result. The version-2 run envelope may
  record an operator-assigned run ID, declared `live_lab` mode, matrix digest,
  and timezone-aware start/completion bounds.
- Treat the envelope, run ID, mode, digest, case records, and timestamps as
  caller declarations unless independently corroborated. A digest match
  establishes which matrix bytes the calculator loaded; it does not prove
  those bytes were used during execution. A declared `live_lab` mode does not
  prove that a live run occurred.
- Preserve one case record per matrix ID and one record per control. Keep
  actual `injected_at`, `detected_at`, and verified `recovered_at` values
  distinct. Do not create timestamps or outcomes that were not observed.

### Source and observation evidence

For each evidence item, record an access-controlled reference, source
identity, source sample timestamp (when supplied by the source), query or
evaluation timestamp, and evaluator observation time separately. Include the
Kubernetes context, namespace, resource kind/name, Pod UID where applicable,
event reason/message, and source timestamps needed to correlate records.
Capture enough before/after status to distinguish a baseline from a change.
Use bounded, relevant records; redact secrets and avoid full prompts,
credentials, and unnecessary personal data.

Prometheus/Metrics API samples must retain their source sample timestamps
separately from query evaluation and observation time. A query timestamp does
not establish raw scrape freshness. If the source is unavailable, malformed,
contradictory, outside the approved observation window, or cannot be tied to
the lab resource, record the limitation; do not translate it into healthy
telemetry or a successful detection.

The Kubernetes observer is a read-only, request-driven collector of lab
Pods, Events, and Deployments; it is not a continuous watcher. Record the
actual observation time and preserve event timestamps and Pod UID. For
deleted Pods, retain an event only when the observer's bounded lookback and
identity logic provide the relevant evidence; do not claim a finding from an
event that was not actually observed.

Collector/Loki evidence is optional and scenario-dependent. The current
observability documentation describes a separate local application-log
pipeline; it does not establish Kubernetes scenario log delivery or require
Loki for this matrix. If a separately approved collection plan uses it,
record the source, query interval/evaluation time, bounded result reference,
and limitations. A query result or missing record does not by itself prove
which delivery stage accepted or lost a log.

### Classification, response, and audit

- Preserve the observer output separately from classifier output. Record the
  classifier's category, confidence, severity, target identity, and evidence
  references exactly as returned. Compare to the expected category only
  during evaluation; never replace an actual result with the matrix label.
- Preserve diagnosis/RCA as advisory input separately from the deterministic
  policy decision. Record policy rules/reasons, whether approval was required,
  the approval request and decision identity/time/outcome, and any execution
  status as distinct events.
- Keep remediation in dry-run mode. Record planned command/action and
  `not_executed` distinctly; do not label dry-run as recovery. Any proposal
  for a live action requires separate authorization and is outside this plan.
- If a separately approved action ever occurs, capture its approved action,
  operator/approver, execution outcome, and independent post-action
  verification. Recovery is not established by a successful command alone.
- Preserve relevant audit event references and the correlated incident/run
  IDs. The repository supports audit and approval records, but caller-supplied
  references or benchmark fields alone do not prove persistence or
  authenticity. An independent reviewer must check the referenced records
  against the source system and approved scope.

### Evidence disposition

The evidence custodian preserves raw or appropriately redacted source
artifacts and links them to the run/case without altering original results.
The independent reviewer, named in the authorization record, checks
environment identity, timestamps, resource/UID correspondence, scenario
execution, classification, policy/approval sequence, dry-run boundary,
cleanup, and deviations before accepting a case. Acceptance criteria and
disposition for missing or disputed evidence must be approved before the run;
this plan sets no freshness threshold or numerical quality threshold.

Missing, malformed, contradictory, unavailable, or out-of-scope evidence
must be marked as such and excluded where the result schema requires it.
Never infer successful injection from the command alone, detection from
injection, correct target from category, freshness from parseable
timestamps, policy safety from schema coverage, or recovery from a dry-run.

## 4. Stop, cleanup, and rollback

Before a run, the authorization must define an operator-approved response
for every stop condition below, including who acts, which existing
simulator cleanup is permitted, and how restoration is verified. No action
is automatic or guaranteed.

| Stop condition | Required operator-approved response |
| --- | --- |
| Any production or out-of-scope resource, context, credential, or workload is accessed or may be affected. | Stop the run; do not inject further. Name the escalation owner and isolation/containment response before execution. |
| Baseline health deteriorates beyond the operator-approved limits, or node/storage/memory pressure appears. | Stop the current scenario and use only the specifically approved cleanup/rollback; escalate and verify baseline before any further action. Limits and response are operator decisions. |
| Critical evidence is missing, malformed, contradictory, or cannot be correlated to the approved resource/run. | Stop scoring and further scenarios. Preserve what exists, mark the case incomplete, and obtain reviewer disposition; do not repair the record by inference. |
| A state-changing action occurs without explicit approval, or remediation leaves dry-run mode without separate authorization. | Stop the run and further state changes; invoke the preapproved escalation and containment procedure. Do not assume rollback is safe or automatic. |
| Required Kubernetes, Prometheus/Metrics API, or other approved observability is lost. | Stop the affected scenario. Apply only an approved cleanup, then have the operator decide whether evidence is sufficient or the run must be terminated. |
| Approved time or resource bounds are exceeded or cannot be monitored. | Stop at the approved boundary and execute the named cleanup/rollback response; do not extend the window without new approval. |
| Cleanup fails, the simulator remains active, deployment restoration fails, or recovery/baseline cannot be verified. | Stop all further scenarios. Escalate to the named operator; continue only explicitly approved recovery steps and record the lab as not restored until verified. |
| An unexpected simulator or target workload is present, or the target/variant does not match approval. | Do not inject; if already active, stop and follow the approved cleanup. Reconcile scope with the owner before any new authorization. |

For CPU, memory, and disk scenarios, the supported cleanup scales the
corresponding simulator deployment to zero. For deployment failure, supported
cleanup restores the configured healthy image and checks rollout status. The
pod-crash scenario is self-healing and has no cleanup mutation; recovery must
be verified. After every case, independently verify simulator deployments are
scaled to zero, `demo-web` is healthy, the node/lab baseline is within
approved bounds, and no unresolved incident remains. A cleanup command
success is not itself proof of restoration.

## 5. Readiness sign-off gate

All mandatory rows must have named owners, attached evidence/approval
references, and an explicit accepted status. Any blank, pending, disputed, or
failed row keeps the outcome **BLOCKED**.

| Sign-off dimension | Evidence required before authorization | Current status |
| --- | --- | --- |
| Lab isolation and ownership | Verified cluster/context/namespace identity, owner, allowed resources, and production/unrelated-resource exclusion. | **BLOCKED** — operator decision required |
| Scenario-by-scenario approval | Individual approval for each selected ID, expected impact, variant preconditions, resource bounds, and retry policy. | **BLOCKED** — no scenario approved |
| Authorization and access boundaries | Named start/approve/stop/review identities, permitted credentials and access, separation of duties, escalation contact. | **BLOCKED** — operator decision required |
| Baseline and observability validation | Approved health limits and captured preflight; required sources ready with timestamps and failure behavior defined. | **BLOCKED** — not validated for a scheduled run |
| Evidence collection and independent review | Evidence custodian, retention location, source/artifact plan, redaction, reviewer, and acceptance/disposition criteria. | **BLOCKED** — operator decision required |
| Stop, cleanup, and rollback readiness | Stop authority, scenario-specific supported cleanup, rollback/restoration steps, and verifiable recovery criteria approved. | **BLOCKED** — not authorized or demonstrated for this plan |
| Result schema and evaluation compatibility | Confirm selected cases/controls can be recorded without invented IDs, provenance, timestamps, approvals, or execution; preserve raw records and report exclusions. | **BLOCKED** — no live records reviewed |
| Final operator authorization | Recorded final approval naming environment, scenario IDs, window, roles, stop contact, evidence retention, and cleanup/rollback. | **BLOCKED** — planning phase grants no authorization |

This checklist is a template, not proof that the actual lab is ready. The
outcome remains **BLOCKED** until the required decisions and evidence are
completed and independently reviewed.

## 6. Open operator decisions

- Who owns and verifies the exact lab environment, namespace, and resource
  isolation?
- Which individual scenario IDs and controls, if any, are approved, and what
  impact/resource bounds are acceptable for each?
- Which identities may start, approve, stop, clean up, and independently
  review? Who is the escalation contact?
- What baseline-health and observability criteria block a scenario, without
  treating unavailable telemetry as healthy?
- What is the bounded test window and maximum duration?
- Where will evidence be retained, for how long, under whose access control,
  and with what redaction requirements?
- What stop, cleanup, restoration, and recovery actions are approved, and
  what evidence demonstrates the lab baseline is restored?
- Is any state-changing remediation to be considered in a future phase?
  Unless separately approved, remediation remains dry-run and live action is
  prohibited.
- What evidence acceptance rules and metric thresholds are approved? No
  freshness or performance threshold is introduced here.

Until those decisions are recorded, independently reviewed, and covered by
final authorization, **do not schedule or execute a live-lab evaluation**.
