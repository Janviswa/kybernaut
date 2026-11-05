# Kybernaut Evaluation Protocol

## Objectives and environment

This protocol separates deterministic code validation from live infrastructure
benchmarking. Synthetic tests use fixtures, fake observations, and temporary
SQLite databases. They do not run Kubernetes commands, invoke the failure
simulator, call an external LLM provider, or perform remediation.

For a code-grounded audit of current metric correctness, schema enforcement,
and evaluation gaps, see the
[Evaluation Readiness Audit](./EVALUATION_READINESS_AUDIT.md).
For the fail-closed preflight, scenario, evidence, and sign-off checklist
required before a separately authorized live-lab evaluation, see the
[Controlled Live-Lab Evaluation Plan](./LIVE_LAB_EVALUATION_PLAN.md). The plan
is blocked and grants no execution authorization.

The live benchmark is a separate, explicitly authorized activity. It requires
an isolated lab, pre-flight health checks, one scenario at a time, supported
cleanup, and recorded evidence. Keep remediation in dry-run mode; any live
remediation is outside the controlled plan and requires separate
authorization.

The reported full test-suite result of 311 passed before the local
observability-stack changes is historical evidence only.
It is not a 20-scenario benchmark result. The 20-case benchmark has not been
shown to have been executed in the live lab. The local synthetic benchmark
described below evaluates deterministic fixtures, not the live scenarios.
Local synthetic results do not establish live-lab readiness or production
readiness.
See the audit's separate readiness decisions for those dimensions.

## Prometheus feature freshness

The feature collector retains the Prometheus instant-query timestamp for each
feature and emits `sample_timestamps` plus the latest `observed_at` timestamp
on complete service rows. By default, each sample must be no more than 30
seconds old and timestamps within a service row must be no more than 10 seconds
apart. Configure these limits with
`KYBERNAUT_PROMETHEUS_MAX_SAMPLE_AGE_SECONDS` and
`KYBERNAUT_PROMETHEUS_MAX_SAMPLE_SKEW_SECONDS`; both must be positive finite
numbers. Rows with missing features, stale or future timestamps, or excessive
timestamp skew are omitted, so downstream evaluation treats them as unavailable
telemetry rather than as an anomaly.

This validates the timestamps returned by the instant queries; it does not
prove that every underlying raw scrape used by a PromQL expression was fresh.
Prometheus lookback and staleness behavior, and real scrape delivery, require
separate monitoring-environment validation.

## Scenario matrix

`benchmark/scenarios.json` is the canonical 20-case matrix. It contains four
cases for each expected category: `cpu_spike`, `memory_leak`, `pod_crash`,
`disk_full`, and `deployment_failure`. Each case records its `scenario_id`,
simulator scenario, expected category, contextual variant, and expected
classification outcome.

The simulator applies the same supported injection for repetitions of a
scenario. Variant labels such as low traffic or replica count are contextual
conditions; the simulator does not establish or enforce them. Record and
confirm those conditions separately before counting such a case as run.

Pod-crash behavior is a signal, not Pod deletion: the simulator selects the
first eligible Ready `demo-web` Pod returned by Kubernetes, validates its
container, checks PID 1 identity, and sends `kill -9 1`. The benchmark matrix
now describes this behavior rather than claiming it deletes either the first
or second Pod. Its strict fresh-evidence checks remain required. Live
pod-crash validation is pending: the previous authorized attempt did not
produce fresh crash evidence, and that authorization is exhausted.

Each scenario has `expected_outcome` equal to
`classified_as_expected_category`. This is a target for evaluation, not a
claim that detection succeeded. A missed or incorrectly classified incident
must be recorded as observed; do not replace it with the expected result.

## Healthy controls

The matrix declares five healthy control windows, each 15 minutes long:

1. Healthy baseline, with no simulator workload active.
2. Healthy services after CPU scenario cleanup.
3. Healthy services after memory scenario cleanup.
4. Healthy services after disk scenario cleanup.
5. Healthy services after deployment image restoration.

Each control expects no alert and category `unknown`. Record whether any alert
was raised. A control with an alert contributes a false positive. If controls
are omitted from a metric input, the report is incomplete and validation
fails. A complete report requires the exact five IDs declared in the matrix.
Control records require `control_id`, boolean `alert_raised`,
`observed_category: "unknown"`, timezone-aware `started_at` and `ended_at`
with end not before start, a string-list `evidence`, and `failure_reason`
(string or null). The validator does not impose a minimum control duration or
calculate a false-positive rate by control duration.

## Recorded results schema

Store a JSON object with `cases` and `controls`, using one case record for each
of the 20 matrix IDs. All timestamps are UTC ISO-8601 values with an explicit
timezone. Preserve actual observed evidence; use empty evidence only when no
evidence was observed. `failure_reason` is required as a field and is `null`
when no failure occurred.

The top-level `schema_version` is optional for compatibility. Its absence is
accepted as a legacy result. Integer version `1` is the F.14 compatible shape
with optional target, provenance, and policy/approval fields. Integer version
`2` requires the run envelope described below. Other types/versions fail.
Unknown top-level and record fields remain tolerated, while malformed values
for recognized fields fail validation. Neither version makes optional
evidence mandatory or interprets absence as success. Existing `cases` and
`controls` consumers can continue reading either version.

### Version 2 run envelope

Version 2 adds a single `run` object beside the existing `cases` and
`controls`; it does not alter those records' meanings:

```json
{
  "schema_version": 2,
  "run": {
    "run_id": "operator-assigned-opaque-id",
    "evaluation_mode": "synthetic",
    "scenario_matrix_sha256": "64 lowercase hexadecimal characters",
    "started_at": "2026-01-01T00:00:00+00:00",
    "completed_at": "2026-01-01T00:20:00+00:00"
  },
  "cases": [],
  "controls": []
}
```

`run_id` is a caller-assigned, non-empty identifier for correlation; the
calculator does not guarantee global uniqueness. `evaluation_mode` is the
declared mode (`synthetic` or `live_lab`), not independently verified
execution. `scenario_matrix_sha256` is SHA-256 over the exact bytes of the
scenario-matrix file loaded for this calculation (not a normalized JSON
representation). It binds this report input to those loaded bytes but cannot
prove that those bytes were used during an earlier execution or that the
reported events actually occurred. `started_at` and `completed_at` are
timezone-aware run bounds; completion must not precede start. Recorded case
injection/detection/recovery times, control window times, policy decision /
approval / execution times, and evidence query-evaluation / observation
times, when present, must be within those bounds. Source sample times are not
constrained to the run interval because a query may legitimately return
historical data.

The case and control arrays remain validated against the exact canonical IDs,
with duplicates, omissions, extras, and category mismatches rejected. If an
individual record includes optional `run_id`, it must match the envelope.
The envelope itself contains one run only; reports from different runs must
not be concatenated and scored as one population. `run_metadata` in metric
output is separate from performance metrics and labels enveloped declarations
as unverified. Legacy and version-1 results report legacy metadata without
invented IDs, mode, digest, or timestamps.

Use an assigned run ID and the exact matrix file used for the reported result.
Do not backfill these values onto historical results. A declared
`evaluation_mode: "live_lab"` is not evidence that live infrastructure was
used; live-lab claims require separately authorized execution records and
independent evidence review.

```json
{
  "schema_version": 1,
  "cases": [
    {
      "scenario_id": "cpu-spike-1",
      "expected_category": "cpu_spike",
      "observed_category": "cpu_spike",
      "observed_target_id": null,
      "detection_result": "detected",
      "injected_at": "2026-01-01T00:00:00+00:00",
      "detected_at": "2026-01-01T00:00:30+00:00",
      "recovered_at": null,
      "evidence": ["record the observed metric, event, or state"],
      "remediation_outcome": "not_executed",
      "remediation_result": null,
      "verification_outcome": "not_run",
      "failure_reason": null,
      "rca_json_valid": null,
      "llm_latency_ms": null,
      "llm_tokens": null,
      "evidence_provenance": []
    }
  ],
  "controls": [
    {
      "control_id": "healthy-control-1",
      "alert_raised": false,
      "observed_category": "unknown",
      "started_at": "2026-01-01T00:00:00+00:00",
      "ended_at": "2026-01-01T00:15:00+00:00",
      "evidence": [],
      "failure_reason": null
    }
  ]
}
```

Allowed case values:

- `detection_result`: `detected`, `missed`, or `not_measured`. Detection is
  whether an alert was observed; category correctness is measured separately.
  An explicit `not_measured` result requires null `detected_at`; all controls
  must also use null `alert_raised`. When detection is not measured,
  precision, recall, F1, and false-positive counts are null, not zero.
- `observed_category`: one of the five categories, `unknown`, or `null` if no
  classification was produced.
- `remediation_outcome`: `executed`, `not_executed`, or `not_applicable`.
  A successful dry-run is `not_executed`, never live remediation success.
- `remediation_result`: `success`, `failed`, or `null`.
- `verification_outcome`: `verified`, `not_verified`, `not_run`, or
  `not_applicable`. Dry-run must be `not_run` because no action occurred.
- `recovered_at`: set only when recovery was actually verified.
- A `verified` outcome is valid only when `remediation_outcome` is `executed`,
  `remediation_result` is `success`, and `recovered_at` is a valid
  timezone-aware timestamp. A non-executed outcome cannot claim recovery.
  Non-executed results use `not_run` for a dry-run or `not_applicable` when no
  action/verification was applicable.
- `failure_reason`: concise reason for a missed detection, failed action,
  failed verification, or other unsuccessful case; otherwise `null`.

The metric runner loads `benchmark/scenarios.json` as its canonical matrix
(or a matrix supplied with `--scenarios`) and requires every scenario ID to
have its declared expected category. It also requires exactly the declared
healthy-control IDs and validates their recorded fields described above.
Missing, duplicate, extra, or malformed scenarios/controls make the report
incomplete. The runner requires all 20 exact `scenario_id` values, four cases per
expected category, a valid `detection_result`, and a `detected_at` timestamp
exactly when detection_result is `detected`. `injected_at` must be a
timezone-aware timestamp string; optional `detected_at` and `recovered_at`
must be null or valid timezone-aware timestamp strings. Malformed non-null
timestamps are rejected even on missed cases, and detected/recovery timestamps
before injection are rejected.

`llm_latency_ms` and `llm_tokens` must be null or finite, nonnegative,
non-boolean numbers. `rca_json_valid` must be boolean or null. Invalid
provided values fail validation rather than being silently omitted.

If a supplied matrix is missing, unreadable, malformed, or internally
inconsistent, the CLI prints an `INCOMPLETE` validation message and exits 2.
The same exit-2 behavior applies to a malformed or unsupported supplied
`schema_version` and malformed recognized result fields.

It calculates detection
precision/recall/F1, MTTD, verified MTTR, category accuracy and macro-F1,
remediation/verification rates, and optional LLM latency/token statistics.
Detection metrics count an alert as detected even if its category is wrong;
classification metrics measure category correctness separately. MTTD and
MTTR include detected or verified records respectively; MTTR additionally
requires `verification_outcome: "verified"`. Contradictory negative intervals
fail validation rather than being omitted.
The validator rejects a recovery claim unless it records an executed,
successful action and valid recovery timestamp. The current result fields do
not include a separate execution-mode field, so `not_executed` plus
`not_run` is the recorded dry-run shape; the validator cannot distinguish a
dry-run from another non-executed, unverified result beyond those fields.
Legacy scalar metrics remain available. Additional `timing.mttd` and
`timing.verified_mttr` objects report summed-seconds numerator, mean,
denominator/sample count, missing count, and excluded count. RCA validity,
LLM means, remediation success, and verification success similarly expose
count objects (`rca.json_validity_counts`, `llm.latency_counts`,
`llm.token_counts`, `remediation_success_counts`, and
`verification_success_counts`). Empty denominators have a null rate/mean,
not a zero measurement. For RCA/LLM metrics, missing counts are absent/null
observations; for action rates, non-executed cases are reported as excluded
from the executed-action denominator.

### Classification, target, evidence, and safety metrics

Legacy `classification.accuracy`, `classification.macro_f1`, and
`classification.per_class_f1` retain their original meaning: the 20 injected
cases only, with macro-F1 over the five incident categories. The additive
`classification_metrics` covers those 20 cases plus the five declared healthy
controls, with actual labels from `expected_category` in cases and `unknown`
for controls. Predictions come only from `observed_category`.

Classification is intentionally independent of detection: a missed case
whose category was nevertheless recorded remains a category prediction, and
is separately counted in `missed_detection_count`. An explicit `unknown`
prediction for a positive incident is not correct; it is a false negative for
the actual category and a false positive for `unknown`. A null prediction is
`unclassified`, not `unknown`, and is a false negative for its actual
category. `classification_metrics` reports the six-category confusion
matrix, per-category support/predicted counts, TP/FP/FN, precision/recall/F1,
and accuracy/macro-F1 counts. Precision is null when a category has no
predictions; recall is null only when its actual support is zero; F1 is null
only when its denominator is zero.

`expected_target_id`, when deliberately supplied in a canonical scenario,
and `observed_target_id`, when supplied in a result, are non-empty opaque
strings compared exactly. No target is inferred from other fields. A missing
expected label or observed ID is excluded and counted by reason; a supplied
malformed observed ID fails validation. The current canonical matrix defines
no expected target IDs, so reports explicitly return `status: "not_measured"`
with zero denominator; this is not a target-accuracy result.

An optional per-record `evidence_provenance` list records provenance
separately from the evidence text/reference. New entries use:

```json
{
  "evidence_ref": "local-artifact:case-1/query-1",
  "source_identity": "prometheus:lab-cluster",
  "status": "available",
  "source_sample_timestamp": "2026-01-01T00:00:00+00:00",
  "query_evaluation_timestamp": "2026-01-01T00:00:02+00:00",
  "observation_time": "2026-01-01T00:00:03+00:00"
}
```

`source_identity` and `status` are required for a complete new entry.
`source_sample_timestamp` (time of the source sample) and
`query_evaluation_timestamp` (time the query was evaluated) are separate
nullable timestamps; their keys must be present to represent unavailable
timestamps explicitly. `observation_time` records when the evaluator
collected the evidence and must be timezone-aware. `evidence_ref` is an
optional non-empty pointer to a retained, access-controlled artifact; do not
put secrets, prompts, or unnecessary personal data in it. Existing F.13
`source`/`source_observed_at`/`query_timestamp`/`timestamp_provenance` entries
remain accepted as a legacy provenance shape.

Malformed supplied values fail validation; absent/incomplete metadata is
reported as coverage, not converted to success. The calculator reports
sample/query/observation timestamp and reference coverage separately.
Timestamp parsing does not authenticate provenance or establish freshness.
No freshness threshold is approved, so `freshness_assessed_entry_count`
remains zero; an instant-query evaluation timestamp also cannot prove
underlying scrape freshness.

An optional `policy_approval` object separates `policy_decision`
(`allow`, `deny`, `require_approval`), boolean `approval_required`,
`approval_outcome` (`not_required`, `pending`, `approved`, `denied`, `expired`),
`execution_status` (`not_executed`, `executed`, `failed`),
`verification_result`, and decision/approval/execution timestamps. A required
approval must be approved before an execution attempt; the object must agree
with the existing remediation and verification fields. Optional
`audit_event_refs` is a list of non-empty opaque references. Contradictory
states and malformed supplied fields fail validation.

`decision_at` is required. A terminal approval outcome requires
`approval_decided_at`; it cannot precede `decision_at`. An executed or failed
attempt requires `execution_started_at`, not earlier than the policy decision;
when approval is required, execution must also follow the approval timestamp.
An execution timestamp is invalid when `execution_status` is
`not_executed`. `execution_status: "executed"` maps to
`remediation_outcome: "executed"` plus `remediation_result: "success"`;
`"failed"` maps to an executed action with a failed result; and
`"not_executed"` maps to a non-executed/not-applicable outcome. The
`verification_result` must equal the record's `verification_outcome`.

`policy_approval_safety` reports the coverage and counts of these
schema-consistent, caller-recorded case states. If absent, cases are excluded and
the status is `not_measured`; if present, the coverage substatus may be
`schema_validated`, but policy safety remains `not_measured`. These fields do
not claim that runtime policy enforcement, approval, audit persistence,
execution, or recovery actually occurred. Independently corroborated event
evidence would be required for those claims.

The current calculator's metric definitions, denominators, and validation
gaps are audited in
[EVALUATION_READINESS_AUDIT.md](./EVALUATION_READINESS_AUDIT.md). In
particular, the F.11 changes make malformed and negative timing values fail
closed and expose sample counts; F.12 binds scenario/control records to the
canonical matrix. F.13 added classification details and initial target,
provenance, and policy-state coverage. F.14 adds optional result versioning
and explicitly typed target/provenance/policy metadata. F.15 adds the
optional run envelope; it identifies declared mode and matrix bytes but does
not authenticate execution. No phase claims freshness or runtime safety
unsupported by evidence.

## Running synthetic validation

From the repository root:

```bash
.venv/bin/python -m pytest -q tests/test_benchmark_metrics.py tests/test_incident_response_graph.py
```

The synthetic lifecycle test supplies Kubernetes-like evidence as an in-memory
fixture, checks classification and approval, resumes the approval through the
real workflow graph with the remediation helper in dry-run mode, and inspects
the isolated audit records. It asserts that a dry-run returns
`recovered: false` / `not_executed`, creates no `verification_completed` or
`incident_recovery_verified` event, and is not described as successful live
remediation.

This test exercises the graph's truthful dry-run terminal path. It does not
invoke the recovery verifier, because the graph intentionally skips actual
recovery verification when no action was executed. The verifier is covered
separately with synthetic healthy and unhealthy observations.

## Reproducing the synthetic benchmark

Run the deterministic, local-only harness from the repository root:

```bash
.venv/bin/python scripts/run_synthetic_benchmark.py
```

It writes the raw, version-2 result artifact to
`benchmark/results/synthetic-benchmark.json`. Override the destination with
`--output PATH`, or supply a compatible scenario matrix with `--scenarios
PATH`. The runner uses the canonical 20 scenario IDs and five control IDs. It calls
the existing `diagnose_service` function with a locally patched synthetic
Prometheus query function, then calls the existing deterministic
`classify_incident` and `evaluate_policy` functions with in-process evidence.
It validates the artifact with the existing `compute_metrics`
schema/calculator. The artifact includes
the run envelope, matrix digest, per-case provenance, raw results, computed
metrics, and a summary. `scripts/run_benchmark.py --results
benchmark/results/synthetic-benchmark.json` can independently recalculate
the existing schema metrics.

For this mode, schema field `injected_at` records the synthetic fixture
creation time, not a real failure injection. Healthy controls are point-in-time
synthetic fixtures; they are not 15-minute observed windows.

The recorded run on 2026-10-22 classified **20/20** synthetic fixtures
correctly and matched **16/16** applicable policy expectations. The mocked
diagnosis path reported 18 available results, one missing-evidence result,
and one unavailable result. Detection, live execution, and recovery remain
not measured. These values describe this fixture run only.

The summary reports:

- **Classification accuracy:** classifier output versus the matrix's
  expected category for the 20 positive synthetic fixture cases.
- **Policy contract match:** actual deterministic policy result versus an
  explicit expected decision. CPU fixtures have no actionable Kubernetes
  target and are not policy-evaluated. One case checks denial when deployment
  state is missing; one uses a clearly marked synthetic auto-eligibility
  policy profile. Neither case executes an action.
- **Diagnosis fixture availability:** counts of `available`,
  `missing_evidence`, and `unavailable` synthetic diagnosis states. These
  values are fixture coverage, not a measured diagnosis integration result.
- **Detection, execution, and recovery:** explicitly not measured. No alert
  source, failure simulator, cluster, live remediation, or recovery verifier
  is invoked. Dry-run and live-recovery metrics therefore remain null or
  `not_exercised`, never synthetic success.

The generated positive fixture inputs are intentionally compatible with the
existing classifier rules. The CPU fixtures exercise only the classifier's
telemetry-candidate branch; memory fixtures use OOM evidence, not a measured
memory-growth trend. The matrix's contextual live variants (traffic,
replicas, repeats, and cleanup) are not executed or enforced. Thus 20/20
synthetic classification accuracy, when obtained, measures this harness's
fixture-to-classifier behavior only; it is not live monitoring accuracy or
evidence that all five live scenario types are operationally validated.

## Combined reproducible evaluation report

Generate a unique offline report bundle from the active anomaly comparison
and deterministic incident/policy fixtures:

```bash
.venv/bin/python scripts/run_reproducible_evaluation.py
```

The command creates a new directory under
`benchmark/results/evaluation-runs/` containing `evaluation_report.json`,
`evaluation_report.md`, and `resume_metrics.md`. It refuses to overwrite an
existing run directory. The report records revision, worktree-state digest,
environment, dataset/baseline hashes, labels, configuration, seeds, confusion
counts, formulas, and explicit status/reason fields for metrics not measured.
Only tested numerical results are included in the resume summary.

The combined report rebuilds the Isolation Forest/MAD/hybrid comparison and
the synthetic classifier/policy fixture run in process, then validates both
with their existing artifact validators. It does not call pytest, a model
provider, a running API, Prometheus, Kubernetes, the fault simulator, or a
remediation executor. Consequently live MTTD, incident persistence and
end-to-end rates, operational safety rates, LLM/RCA quality, and API latency
remain `not_measured`; passing unit tests are coverage evidence, not an
empirical safety rate.

For version-2 `live_lab` result inputs, every case must include a unique
`trial_id` and `injection_status`. Only successfully injected cases with a
measured detected/missed outcome enter detection denominators. Failed
injections must be `not_measured` and have a failure reason. Successfully
injected cases require a nonnegative `injected_monotonic_ns`; detected cases
also require `detected_monotonic_ns` and UTC `injected_at`/`detected_at`.
Monotonic nanoseconds are used for durations; UTC timestamps are audit
timestamps and must fit within the declared run envelope. MTTD reports
mean/median/p95/min/max, sample and exclusion counts, missed/failed scenario
IDs, and per-case scenario values. Existing legacy/synthetic records retain
their UTC-difference behavior and are labeled accordingly; they are not
presented as monotonic live measurements.

The command does not create live results or authorize fault injection. Before
any lab mutation, show the exact operation, target, namespace, cleanup, and
rollback procedure and obtain explicit owner approval.

## Synthetic anomaly point evaluation

The active application path is
`app.services.anomaly_runtime.evaluate_service_anomaly`, which fits
`DirectionAwareAnomalyPolicy` from
`data/telemetry_baseline_v6.csv` and uses `POLICY_THRESHOLD = 5.0`.
`TelemetryAnomalyDetector` (Isolation Forest) exists but is not used by that
runtime path. The direction-aware policy marks statistically high CPU,
memory, and latency deviations actionable; low deviations and deviations in
other features are informational. A separate fixed `p95_latency_seconds >
1.0` operational finding is also returned by the policy.

At runtime, the v6 CSV is checked against its exact SHA-256
(`777724eb6aae20da2104fef327dd691ab31a542be06f0c45fc5f66d8cfcac1a1`),
ordered schema, expected services, minimum per-service coverage, timezone-aware
chronological timestamps, unique service/timestamp pairs, and finite,
nonnegative feature values. The expected version and digest are pinned in
release-controlled runtime code; a mutable manifest beside the CSV would not
provide an independent integrity anchor. Validation failure clears all fitted
policies and makes anomaly evaluation unavailable. The hash verifies file
identity, not capture environment, collection provenance, or label quality;
those remain unverified.

Reproduce and validate the synthetic point-level benchmark:

```bash
.venv/bin/python scripts/evaluate_anomaly_benchmark.py
.venv/bin/python scripts/evaluate_anomaly_benchmark.py \
  --validate benchmark/results/direction-aware-anomaly-benchmark-v2.json
```

The default path is an approved-baseline engineering check: it reuses the
runtime's shared v6 baseline validator and refuses any digest/version mismatch.
Use `--exploratory-baseline PATH` only when intentionally evaluating a
structurally valid non-approved baseline. That mode records its exact digest
and is labeled `exploratory`; it does not update or replace the approved
identity and is not qualified. The MAD holdout scripts accept the same
`--exploratory-baseline PATH` option; their default and `--baseline PATH`
inputs must satisfy the approved contract.

The artifact includes each point's ground-truth label, features, detector
output, service/time, baseline digest, threshold/configuration, synthetic
assumptions, and separate unavailability records. The evaluator uses a
chronological 70/30 split of the baseline per service: the first 70% trains
the actual policy; the later 30% is treated as normal holdout under the
explicit assumption that the stored baseline is normal. Synthetic points use
a fixed random seed, 2.5 robust-scale bounded noise, 8-scale sudden steps,
and 0.75-scale gradual CPU drift at 30-second intervals. Missing and
non-finite observations are recorded unavailable, not relabeled normal.
Features and the 5.0 score threshold come from the existing active policy;
the evaluation does not tune production defaults.

The primary point prediction is positive when the policy returns either an
actionable candidate or an informational deviation. Confusion metrics count
only labeled, evaluated points. Actionable-candidate confusion metrics are
reported separately because an unusual low-direction reading is not
necessarily eligible for action. Undefined metrics (including recall or
false-positive rate for absent classes) are null. The gradual-drift delay is
measured from the first labeled drift point to the first predicted-anomaly
point, only for generated drift episodes. It is not incident detection or
response latency.

On 2026-10-22 the run contained 184 points: 176 labeled/evaluated and 8
unavailable. Point-level confusion was TP 40, FP 4, TN 104, FN 28, yielding
precision 0.909091, recall 0.588235, F1 0.714286, and false-positive rate
0.037037. The actionable-candidate confusion was TP 32, FP 2, TN 106, FN 36.
All four gradual CPU drift episodes were detected with mean delay 180 seconds.
These values are reproducible measurements of this synthetic/assumed dataset,
not live detector quality. The dataset does not model disk availability:
`available_disk_space` is explicitly recorded as unsupported because it is
not in the active detector's feature set.

## Three-way synthetic anomaly comparison

The separate three-way artifact compares the existing Isolation Forest scorer,
the direction-aware MAD policy, and their experimental union on the same F.28
synthetic observations, labels, feature rows, timestamps, per-service
chronological split, and unavailable-sample exclusions. Generate it at the
new versioned path without replacing historical artifacts:

```bash
.venv/bin/python scripts/evaluate_anomaly_benchmark.py --three-way
.venv/bin/python scripts/evaluate_anomaly_benchmark.py \
  --validate benchmark/results/three-way-anomaly-benchmark-v5.json
```

For an independent repeat without replacing the versioned artifact, generate
two outputs from the frozen baseline and compare their canonical sample and
metric payloads:

```bash
RUN_DIR="$(mktemp -d /tmp/kybernaut-three-way.XXXXXX)"
.venv/bin/python scripts/evaluate_anomaly_benchmark.py \
  --baseline data/telemetry_baseline_v6.csv --three-way \
  --output "$RUN_DIR/run-a.json"
.venv/bin/python scripts/evaluate_anomaly_benchmark.py \
  --baseline data/telemetry_baseline_v6.csv --three-way \
  --output "$RUN_DIR/run-b.json"
jq -S '{schema_version, provenance, methods, samples, metrics}' \
  "$RUN_DIR/run-a.json" > "$RUN_DIR/run-a-canonical.json"
jq -S '{schema_version, provenance, methods, samples, metrics}' \
  "$RUN_DIR/run-b.json" > "$RUN_DIR/run-b-canonical.json"
cmp "$RUN_DIR/run-a-canonical.json" "$RUN_DIR/run-b-canonical.json"
```

The comparison uses per-service Isolation Forest fitting with 200 estimators,
contamination 0.05, and random state 42; MAD uses the active 5.0 threshold.
The experimental hybrid is the union of the Isolation Forest flag and the
MAD point-anomaly flag. The shared MAD point prediction includes either an
informational deviation or an actionable candidate; MAD actionable-candidate
metrics are reported separately. This comparison does not change runtime
behavior or imply that Isolation Forest or the hybrid is integrated in the
application runtime.

Schema-5 and point-artifact schema-2 runs include reproducibility metadata:
baseline version/digest, evaluator and runtime/policy/detector source hashes,
HEAD revision, a clean/modified/unknown worktree state, Python and available
NumPy/scikit-learn versions, evaluation configuration, random seeds, and label
provenance. Validation checks metadata structure and reports when retained
metadata is historical or its baseline/source revision/hashes no longer match
the current checkout. These hashes and the artifact payload digest are not
authenticated signatures; metadata supports reproducibility review, not
provenance authentication or operational validity. Historical schema-1 and
schema-4 artifacts remain readable and are identified as lacking the new
source-tree metadata; they are not rewritten.

The artifact reports aggregate and per-service confusion counts, precision,
recall, F1, false-positive rate, eligible and unavailable counts, and
unavailability reasons. It records identical per-sample inputs and labels
for all methods, training/evaluation boundaries and their non-overlap,
baseline and generated-dataset hashes, method configuration, random seeds,
schema version, and source identity. Schema 4 and later include a SHA-256
digest over the canonical JSON representation of all artifact fields except
the integrity object itself. The validator recomputes that digest and
recomputes metrics and episode delay from saved predictions. The digest can
detect accidental or uncoordinated changes; it does not authenticate the
artifact's creator, provide tamper-proof provenance, or prove that a detector
was rerun. Reproduction requires rerunning the documented command against the
frozen baseline and comparing predictions and metrics. Detection
delay is defined only for timestamped synthetic episodes: onset is the first
positive-labeled point and detection is the first predicted anomaly at or
after onset. Missed episodes remain counted with null delay; the mean delay
uses detected episodes only.

The labels remain assumed-normal baseline/control points or synthetic
positives by construction. The data has no independently verified
operational labels; temporal holdout is within the same services. Missing and
invalid observations are unavailable for all methods, not normal. Stale and
future telemetry are not modeled in this benchmark. Results are synthetic
comparative evidence only and do not establish production detection quality
or incident-response latency.

### Offline Isolation Forest diagnostic

The separate diagnostic reuses the schema-4 artifact and its frozen baseline.
It does not change the detector wrapper, runtime, labels, or three-way artifact:

```bash
.venv/bin/python scripts/diagnose_isolation_forest.py \
  --artifact benchmark/results/three-way-anomaly-benchmark-v4.json \
  --baseline data/telemetry_baseline_v6.csv \
  --output benchmark/results/isolation-forest-diagnostic-v1.json
.venv/bin/python scripts/diagnose_isolation_forest.py \
  --validate benchmark/results/isolation-forest-diagnostic-v1.json
```

The unscaled control uses the existing 200-tree, contamination-0.05,
random-state-42 wrapper and compares its flags/scores with the saved v4
outputs. Separate diagnostic-only runs change either preprocessing
(per-service `StandardScaler` fit on training rows, with contamination held
at 0.05) or contamination (0.01 and 0.10, with raw features). All use the
same service-specific training rows and eligible frozen evaluation samples.
The report includes decision-function distributions by scenario/label and
feature-value ranges relative to each service's training range. Isolation
Forest scores are joint-model values, not per-feature attributions.

These are exploratory comparisons on synthetic positives and assumed-normal
controls, not independent operational labels, not unbiased performance
estimates, and not a basis for selecting a detector configuration. The
contamination variants are diagnostic, not threshold tuning. The report
records Python, NumPy, and scikit-learn versions because the dependency is not
version-pinned here. Its digest detects accidental or uncoordinated edits; it
does not authenticate authorship or prove that a model was run.

## F.29 False-negative analysis and holdout validation

Run the analysis and produce its separate, machine-readable artifact:

```bash
.venv/bin/python -m scripts.analyze_anomaly_false_negatives
```

Output: `benchmark/results/f29-anomaly-false-negative-analysis.json`. The
script reads and validates the F.28 artifact but does not rewrite it. The
F.28 point confusion matrix recomputes exactly: TP 40, FP 4, TN 104, FN 28
over 176 labeled/evaluated points. Eight unavailable observations are
excluded from all four counts.

### Miss analysis

| False-negative family | Count | Evidence and interpretation |
|---|---:|---|
| Gradual CPU drift | 24 | Six early missed points in each of four service traces; scores range 0.75–4.5, below the strict `score > 5.0` threshold. Each trace has 12 samples at 30-second intervals and becomes detected later. The miss is threshold latency, not missing telemetry. |
| Sudden low request rate | 4 | One point per service; scores 4.263–4.317, below threshold. Low request-rate direction is not an actionable candidate. The synthetic positive label is not independent evidence that this should be treated as an incident. |

All four service copies of each pattern share the same generated scenario
family and should not be treated as independent scenario families. The CPU
false-negative points cover 150 seconds between the first and last missed
sample; detection occurs 180 seconds after the first positive drift label.
F.28 does not specify duration for the four single-point request-rate cases.
The artifact includes each miss's feature, direction, deviation score, value,
and baseline median. No unavailable observations were mislabeled as normal or
as false negatives.

### Split and frozen-threshold validation

F.28 scenario families were kept as the development analysis. Before
evaluating a new set, the active detector and 5.0 threshold were frozen;
no threshold candidate was selected or tuned. The holdout consists of new,
episode-isolated families: gradual memory drift, a concurrent latency/memory
step, stable-center controls, and bounded-noise controls. All points from each
episode stay together, and both episode IDs and scenario-family names are
checked for overlap. The holdout is generated deterministically with seed
2901 using the first 42 chronological baseline rows per service to fit the
unchanged policy.

| Holdout family | TP | FP | TN | FN | Precision | Recall | F1 | FPR |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Gradual memory drift | 12 | 0 | 4 | 24 | 1.0000 | 0.3333 | 0.5000 | 0.0000 |
| Concurrent latency/memory step | 12 | 0 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 | undefined |
| Bounded-noise control | 0 | 0 | 24 | 0 | undefined | undefined | undefined | 0.0000 |
| Stable-center control | 0 | 0 | 12 | 0 | undefined | undefined | undefined | 0.0000 |
| **All holdout points** | **24** | **0** | **40** | **24** | **1.0000** | **0.5000** | **0.6667** | **0.0000** |

The holdout contains 88 labeled points in 16 service-specific episodes. The
mean drift detection delay is 180 seconds across the four gradual-memory
episodes; the concurrent steps are detected at their modeled onset (0
seconds). Delay is only reported for these timestamped episodes. The
class-absent per-family precision/recall/F1 and FPR are undefined, not zero.

### Decision and limitations

No detector change is justified or made. The unchanged threshold's misses
generalize to a different gradual-drift metric in the held-out traces; this
confirms a limitation of pointwise threshold scoring, not a basis for
lowering the threshold. Lowering it would change production behavior and
could alter false-positive rates; the available labels are generated or
assumed, the scenario count is small, and there is no independently reviewed
operational label set or accepted false-positive target. No candidate
threshold was evaluated on holdout. Available-disk-space detection remains
unsupported. These results do not establish live monitoring accuracy,
incident-level detection, or operational recovery.

## Recording and calculating benchmark outcomes

1. Complete and independently review the
   [Controlled Live-Lab Evaluation Plan](./LIVE_LAB_EVALUATION_PLAN.md), then
   obtain separate authorization for the exact environment, scenario IDs,
   window, roles, and cleanup/rollback boundaries. The planning document is
   not authorization.
2. Run only approved scenarios, one at a time, using documented simulator
   commands and cleanup. Do not retry failed pod-crash validation without new
   authorization.
3. Record the observed category, detection status, actual timestamps and
   evidence, action mode/outcome, verification outcome, and failure reason in
   the schema above. Never infer recovery from a successful command or a
   successful dry-run.
4. Record all five healthy controls. Preserve the unmodified raw result file.
5. Calculate metrics from the repository root:

   ```bash
   .venv/bin/python scripts/run_benchmark.py --results benchmark/results.json
   ```

The metrics-only CLI computes metrics from the supplied result file; it does
not execute scenarios or generate results. Do not publish live benchmark
scores until the 20 scenario runs and healthy controls have actually been
recorded and reviewed.

## Known limitations

- The local observability stack is configured in Compose but has not been
  started or runtime-verified. Configuration and synthetic tests do not prove
  Prometheus scraping, log ingestion, or Grafana dashboard operation.
- The deterministic synthetic artifact is not a completed live benchmark.
  No actual live 20-case results file or metrics report is present.
- Pod-crash code validation is complete, but live validation is pending. The
  previous authorized attempt failed to produce fresh restart/termination
  evidence; its authorization is exhausted. No retry is implied or authorized
  by this protocol.
- Synthetic lifecycle inputs do not establish real Kubernetes, container
  runtime, Prometheus, or alert delivery behavior.
- Synthetic freshness tests validate returned sample timestamps only; they do
  not verify real Prometheus scrape freshness or query lookback behavior.
- The simulator does not enforce the matrix's traffic or replica-count
  contextual variants. Operators must confirm and record these preconditions.
- Dry-run records a planned action and an explicit `not_executed` verification
  outcome. It proves neither remediation execution nor recovery.
