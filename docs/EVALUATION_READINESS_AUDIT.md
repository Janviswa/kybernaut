# Evaluation Readiness Audit

**Status: implementation readiness audit, updated 2026-10-22.** This document
assesses the current evaluation path and recorded artifacts. It does not
change runtime behavior, generate benchmark results, or authorize a live-lab
run. For the full proposed
evaluation protocol and future schema direction, see
[EVALUATION.md](./EVALUATION.md) and
[MONITORING_AND_EVALUATION_DESIGN.md](./MONITORING_AND_EVALUATION_DESIGN.md).
The required fail-closed checklist for a future controlled live-lab run is in
[LIVE_LAB_EVALUATION_PLAN.md](./LIVE_LAB_EVALUATION_PLAN.md); its current
outcome is **BLOCKED**, and it grants no execution authorization.

## Scope and evidence

Inspected:

- [Scenario matrix](../benchmark/scenarios.json)
- [Metric validator and calculator](../app/services/benchmark_metrics.py):
  `load_scenario_matrix`, `_timestamp`, `_duration`, `validate_matrix`,
  and `compute_metrics`
- [Metrics CLI](../scripts/run_benchmark.py): `main`
- [Synthetic benchmark runner](../scripts/run_synthetic_benchmark.py):
  `build_synthetic_benchmark`, `validate_synthetic_artifact`
- [Evaluation protocol](./EVALUATION.md)
- [Monitoring and evaluation design](./MONITORING_AND_EVALUATION_DESIGN.md)
- [Anomaly detector](../app/services/anomaly_detector.py):
  `TelemetryAnomalyDetector`
- [Anomaly policy](../app/services/anomaly_policy.py):
  `DirectionAwareAnomalyPolicy`
- [Anomaly runtime](../app/services/anomaly_runtime.py):
  `initialize_anomaly_policies` and `evaluate_service_anomaly`
- [Prometheus feature collector](../app/services/prometheus_features.py):
  `collect_service_features`
- Benchmark validator, anomaly detector/policy/runtime, telemetry integration,
  incident classifier, response graph, workflow, and verification tests,
  including [test_benchmark_metrics.py](../tests/test_benchmark_metrics.py),
  [test_synthetic_benchmark.py](../tests/test_synthetic_benchmark.py),
  [test_anomaly_detector.py](../tests/test_anomaly_detector.py),
  [test_anomaly_policy_service.py](../tests/test_anomaly_policy_service.py),
  [test_telemetry_agent_integration.py](../tests/test_telemetry_agent_integration.py),
  [test_incident_classifier.py](../tests/test_incident_classifier.py),
  [test_incident_response_graph.py](../tests/test_incident_response_graph.py),
  and [test_verification.py](../tests/test_verification.py).

Before F.27, no benchmark result JSON was present under `benchmark/`. F.27
adds [run_synthetic_benchmark.py](../scripts/run_synthetic_benchmark.py),
which writes a version-2 synthetic fixture artifact under
`benchmark/results/`; that artifact is not a live 20-case benchmark result.
The root [result.md](../result.md) records a prior pod-crash
contract-validation outcome. Telemetry baseline CSV files are model inputs,
not labeled evaluation results.

## Current evaluation path

1. `benchmark/scenarios.json` declares 20 positive cases: four each for
   `cpu_spike`, `memory_leak`, `pod_crash`, `disk_full`, and
   `deployment_failure`. It declares five healthy controls. Context
   variants are labels, not simulator-enforced conditions.
2. `scripts/run_synthetic_benchmark.py` runs all 20 matrix IDs through the
   existing deterministic classifier using local fixtures, evaluates policy
   decisions where a target is available, records diagnostic fixture
   availability, and emits schema-version-2 raw records plus metrics. It does
   not invoke the failure simulator, monitor, cluster, or remediation path.
   Live outcomes must still be collected and recorded independently.
3. `scripts/run_benchmark.py` remains a metrics-only CLI. `compute_metrics()`
   loads the canonical scenario/control matrix, validates cases and controls
   against it, then calculates rates and means. The CLI accepts a results JSON
   (and optional `--scenarios` path) and prints the metric object.
4. Version-2 inputs may declare a run ID, `synthetic`/`live_lab` mode, exact
   scenario-matrix file digest, and bounded run timestamps. These are
   caller-supplied declarations, not proof of execution. The input still does
   not authenticate code revision, execution environment, source artifacts,
   policy events, or audit persistence.

## Metric definitions actually implemented

| Output | Implemented calculation and treatment |
| --- | --- |
| Detection | In measured records, `detected` cases are TP; `missed` cases are FN; each control with `alert_raised=true` is FP. Precision is `TP/(TP+FP)` (zero when that denominator is zero); recall is `TP/(TP+FN)`; F1 is their harmonic mean. A detection counts even if classification is wrong or absent. Schema-2 results may explicitly use `not_measured` for all cases and null control alerts; detection rates and counts are then null/not measured rather than fabricated zeros. Exactly the declared control IDs are required; the calculator does not calculate a false-positive rate per control duration. |
| Classification | Legacy accuracy/macro-F1/per-class-F1 remain computed over the 20 positive cases and five incident categories. Additive `classification_metrics` computes a confusion matrix over 20 cases plus five controls and six categories including `unknown`. It uses `observed_category` independent of detection; null is unclassified, unknown on a positive is incorrect, and missed detection is counted separately. It reports per-category TP/FP/FN, support, prediction count, precision/recall/F1 and denominators; undefined precision is null. |
| MTTD | Arithmetic mean of `detected_at - injected_at` for detected cases. Required timestamps are timezone-aware strings; malformed timestamps and negative intervals are rejected. `timing.mttd` reports summed-seconds numerator, denominator/sample count, missing count, and excluded count; missed cases have no detection-time observation. |
| Verified MTTR | Arithmetic mean of `recovered_at - injected_at` where verification is `verified`. Requires a timezone-aware timestamp and, after F.9 hardening, executed successful remediation. Negative intervals are rejected. `timing.verified_mttr` reports numerator, denominator/sample count, missing count, and excluded count. |
| Remediation success | Successful executed records divided by all executed remediation records. Legacy scalar remains; `remediation_success_counts` reports numerator, denominator/sample count, missing count, and non-executed excluded count. Zero denominator yields null. |
| Verification success | Verified records divided by executed remediation records. Legacy scalar remains; `verification_success_counts` reports numerator, denominator/sample count, missing count, and non-executed excluded count. Zero denominator yields null. |
| RCA JSON validity | Boolean `true` values divided by boolean observations. `rca.json_validity_counts` reports numerator, denominator/sample count, missing null count, and excluded count. Non-boolean values are rejected; zero denominator yields null. |
| LLM latency/tokens | Means of `llm_latency_ms` and `llm_tokens`. Values must be finite, nonnegative, non-boolean numbers or null. `llm.latency_counts` and `llm.token_counts` report summed-value numerator, denominator/sample count, missing null count, and excluded count. Zero denominator yields null. |
| Result schema | Legacy unversioned inputs and F.14 integer version 1 remain accepted. Version 2 requires a run envelope with non-empty run ID, declared mode, digest of exact loaded matrix bytes, and timezone-aware run bounds. Result event times are checked within the bounds; canonical case/control IDs still bind records to one run. |
| Target correctness | Additive `target_correctness` compares exact non-empty `observed_target_id` strings with optional canonical `expected_target_id` strings. Missing IDs are excluded; malformed supplied observed IDs fail validation. The matrix has no expected targets, so the metric remains not measured with denominator zero. |
| Evidence provenance/freshness | New entries distinguish `source_identity`, nullable source-sample and query-evaluation timestamps, required evaluator `observation_time`, and optional `evidence_ref`; legacy F.13 entries remain accepted. Missing fields are counted as incomplete and malformed supplied values fail validation. Parseability does not prove freshness; `freshness_assessed_entry_count` remains zero because no approved age threshold or raw scrape-age evidence exists. |
| Policy/approval state coverage | Optional `policy_approval` states are validated against remediation/verification outcomes and decision/approval/execution ordering. Metrics report coverage and state counts; absent objects are excluded. Coverage may be `schema_validated`, but policy safety remains `not_measured`: the calculator cannot prove runtime policy enforcement or audit persistence. |
| Run provenance | Version-2 envelope reports run metadata separately from performance metrics. The exact matrix-byte SHA-256 is checked against the matrix loaded for calculation. Mode and run fields are caller declarations; digest match does not prove which matrix was used during execution, actual mode, evidence authenticity, or global run-ID uniqueness. |

The F.9 validator hardening rejects a verified recovery unless remediation was
executed successfully and a timezone-aware `recovered_at` is present. It also
rejects recovery claims for non-executed remediation and limits
`not_executed` verification to `not_run` or `not_applicable`. The result schema
still has no explicit execution-mode field, so these fields cannot prove that
a non-executed result was specifically a dry run. F.11 additionally rejects
malformed/non-string timestamps and negative timing intervals; validates
optional LLM metrics and RCA flags; and adds auditable counts while
preserving legacy scalar metric fields.

## Findings and readiness

Status meanings: **ready** means the stated narrow function is represented
consistently by the current schema and calculator; **partial** means some
measurement exists but material coverage or interpretation gaps remain;
**blocked** means a trustworthy metric requires data or schema not currently
available; **not measured** means the current benchmark calculator emits no
such measure.

| Area | Status | Evidence and limitation |
| --- | --- | --- |
| Result schema compatibility | partial | Legacy top-level `cases`/`controls` JSON and integer `schema_version: 1` remain accepted. Version 2 requires a run envelope with non-empty ID, declared mode, exact loaded matrix-byte digest, and timezone-aware bounds. Event timestamps are checked against those bounds; code revision, environment, and artifact manifests are not authenticated. |
| Positive scenario matrix | ready | The calculator loads the canonical matrix, validates its internal 20-case/four-per-category shape, and binds each result ID to its declared expected category. Missing or malformed matrix data fails closed. |
| Healthy controls / false positives | partial | Exactly the declared control IDs are required. Control alert, observed `unknown` category, timestamps/order, evidence, and failure reason are validated. No minimum duration or false-positive rate by duration is imposed. A false-positive count is calculated from alerts, but control coverage remains limited to the declared five controls and no live observations exist. |
| Incident detection | partial | Detection outcome counts, precision, recall, and F1 are calculated from submitted labels. There is no recorder or independent alert evidence, and detected status is not cross-checked against observed alert records. It is a report of entered outcomes, not independently measured detection. |
| Categorization | partial | Additive classification metrics now report a six-class confusion matrix over positive scenarios and required healthy controls, with explicit unknown/null/missed handling and category counts. Legacy scalar metrics remain unchanged over positive cases. No live labeled results exist, and the interpretation of macro-F1 including unknown remains an operator review decision. |
| Target/service identification | partial | Exact target correctness is computed only when the canonical matrix explicitly supplies `expected_target_id` and the result supplies `observed_target_id`. The current matrix defines no expected targets, so all 20 scenario cases are excluded with an explicit reason and target accuracy is not measured. |
| Evidence freshness/provenance | partial | Optional F.14 provenance distinguishes source identity/sample time, query evaluation time, evaluator observation time, and evidence reference. Legacy F.13 entries remain accepted; missing fields are coverage gaps and malformed supplied values fail. Freshness assessment remains zero because timestamps cannot authenticate source truth and no age threshold/raw scrape-age evidence is encoded. |
| Policy, approval, audit safety | partial coverage; safety not measured | Optional `policy_approval` states are validated independently and cross-checked against remediation/verification fields, including approval-before-execution timestamps. Coverage and state counts are reported. These are caller-recorded values; the calculator cannot prove runtime policy enforcement or audit persistence, so it emits no safety rate. |
| Execution and verification | partial | Executed/remediation result consistency and verified recovery are validated, and success rates include numerator/denominator and excluded counts. Optional policy/approval fields add caller-reported action/approval states but do not independently attest execution. Non-executed dry-run cannot be distinguished from other non-execution absent explicit mode evidence. |
| Timing | partial | Aware timestamp strings and nonnegative intervals are enforced; malformed values and negative intervals fail validation. MTTD/verified-MTTR include sums, means, sample counts/denominators, missing counts, and excluded counts. Control-window timing is unused, and no operational latency target is defined. |
| RCA/provider | partial | RCA validity flags and LLM numeric fields are strictly typed and bounded; counts and missing observations accompany means/rates. Provider availability/failure categories and RCA quality remain unmeasured; test fixtures do not establish live provider quality. |
| Anomaly detection | synthetic point metrics measured; live not measured | F.28 evaluates the active `DirectionAwareAnomalyPolicy` at its existing `5.0` threshold against chronological assumed-normal baseline holdout plus deterministic labeled synthetic points. It records point confusion, actionable-candidate confusion, and drift delay separately. The labels are synthetic/assumed and do not establish live monitoring quality. `TelemetryAnomalyDetector` is an Isolation Forest with default contamination `0.05`, but is not the runtime scoring path. |
| Verified recovery | partial | The runtime verifier requires available incident evidence, no related incident, desired replicas available, and consecutive healthy observations. Synthetic tests cover recovery and unavailable observations. The benchmark calculator only consumes submitted verification labels/timestamps; it does not independently perform or audit verification. |
| End-to-end benchmark readiness | blocked | No complete recorded 20-case result set plus all five controls is present. The metric code can calculate from a manually supplied complete positive matrix, but current artifacts do not establish completed labeled evaluation. |

### Specific validation ambiguities and defects

1. **[Resolved in F.12] Canonical expected labels are keyed by scenario ID.**
   The calculator loads the declared matrix and rejects swapped or otherwise
   incorrect category mappings, even when category totals remain balanced.
2. **[Resolved in F.12] Controls are complete and bound to the declared set.**
   The calculator requires every declared ID exactly once and validates the
   documented outcome/timestamp fields. No duration minimum is inferred.
3. **[Resolved in F.11] Timing integrity and accounting.** Required and
   optional timestamp types and timezone awareness are validated; malformed
   non-null timestamps, including `detected_at` on missed cases, fail closed.
   Negative detected/recovered intervals are rejected. The output reports
   sums/means, denominators/sample counts, missing counts, and excluded counts.
4. **[Resolved in F.11] Optional metric input validation.** LLM latency/token
   values must be finite, nonnegative, non-boolean numbers or null;
   `rca_json_valid` must be boolean or null. Null observations remain missing
   and are counted. Malformed values no longer disappear silently.
5. **[Partially resolved in F.13] Classification accounting.** New additive
   metrics include `unknown`, healthy controls, null/unclassified predictions,
   and missed-detection counts. Legacy category metrics retain their
   original positive-only definition. Labeled benchmark observations remain
   absent.
6. **[Partially resolved in F.13/F.14] Evidence provenance and target
   coverage.** Exact target identifiers and optional evidence provenance have
   coverage metrics. The canonical matrix still has no expected targets.
   New provenance fields distinguish source sample, query evaluation, and
   evaluator observation timestamps, but cannot establish source truth or
   freshness. Optional artifact references do not authenticate the artifact.
7. **[Partially resolved in F.14] Policy/approval state coverage.** Optional
   policy decision, approval state/order, execution, verification, and audit
   reference fields are validated and counted. Missing data is excluded;
   malformed or contradictory data fails validation. Self-reported fields do
   not prove runtime enforcement or audit persistence, so policy safety remains
   unmeasured.
8. **[Partial in F.15] Run provenance.** Version 2 records a caller-assigned
   run ID, declared `synthetic`/`live_lab` mode, matrix digest, and run
   timestamp bounds. These do not authenticate execution mode, code revision,
   environment, collection ownership, or source artifacts; the matrix digest
   does not prove which bytes were used during a past execution.
9. **[Resolved in F.16] Legacy query timestamp run bound.** The envelope
   validator checks legacy provenance `query_timestamp` as well as the newer
   `query_evaluation_timestamp` against run bounds. Source sample timestamps
   may remain outside the run window when queries return historical data.
10. **[Resolved in F.11] Dry-run exclusion test isolates MTTR.**
   Executed verified records now have distinct recovery durations; the test
   asserts the exact summed duration and sample denominator with a dry-run
   record present.

## Evaluation validity by question

| Question | Current evidence | What is needed for a defensible measurement |
| --- | --- | --- |
| Did an incident get detected? | Manually recorded `detection_result`; synthetic tests cover code paths, not actual alert delivery. | A recorder capturing the triggering observation and event timestamps, plus separately authorized labeled runs. The v2 envelope supplies declared mode and time bounds, but does not independently verify execution or alert delivery. |
| Was the category correct? | Legacy scalar metrics remain on 20 positive cases; additive six-category confusion matrix covers these cases and the five controls, with unknown/null and missed detection reported separately. | Reviewed labels and actual observations; agree whether promotion decisions use the legacy or additive macro-F1. |
| Was the correct target/service selected? | Exact identifier comparison is supported only when the scenario matrix explicitly supplies `expected_target_id` and results supply `observed_target_id`; current expected target denominator is zero. | Approve canonical expected target labels and collect evidence-backed observed IDs. |
| Was evidence fresh and attributable? | Optional structured provenance coverage is validated; valid timestamp syntax is not treated as freshness. No entry is marked freshness-assessed. | Collect trusted source timestamps/provenance and define an approved freshness integration/threshold; independently review live scrape semantics. |
| Were policy and approval safe? | Synthetic graph tests exercise approval, denied/no-action paths, fail-closed audit, and dry-run behavior. Optional policy state is schema-validated and counted, but safety is not measured. | Independently corroborate policy decision, approval order, audit events, execution, and verification before defining a safety metric. |
| Did an action execute and recover the workload? | Synthetic remediation/verification tests; metric schema permits executed/verified labels with consistency checks. | Authorized live evidence, action identity/result, independent post-action observations, timestamps, and preserved audit linkage. |
| Was RCA useful and available? | Synthetic RCA validity/failure tests; optional fields can calculate validity and numeric means. | Labeled RCA-quality rubric, provider status/failure categories, bounded sample counts, and separately authorized provider observations. |
| Did healthy systems remain quiet? | Five proposed controls in the scenario file; no recorded benchmark results. | Complete timed control observations with alert/category/evidence outcomes and run metadata. |

Synthetic unit and contract tests establish behavior under their fixtures and
mocks only. They do not measure live detection accuracy, the real Prometheus
scrape pipeline, Kubernetes evidence timing, provider availability, or
production safety. Any future live-lab results must be separately authorized,
identified as live-lab data, and reported independently from synthetic tests.

## Prioritized remediation plan

These are recommendations, not implementation authorization. Schema changes
should be versioned and retain raw source records.

### P0 — Make result validation fail closed and explain exclusions

**Status: implemented in F.11.** The benchmark validator now enforces the
timestamp and optional-metric constraints below, rejects negative elapsed
times, and reports sample counts while preserving existing scalar outputs.
Regression tests include malformed CLI results without traceback.

- Validate timestamp inputs as strings (or explicit null where allowed) and
  convert malformed values into `BenchmarkIncomplete`.
- Reject negative detected/recovered elapsed times, or represent them as
  explicitly invalid records that cannot contribute to a mean.
- Validate optional numeric metrics as finite, nonnegative, non-boolean
  numbers; require `rca_json_valid` to be boolean or null.
- Return numerator, denominator, and excluded/invalid counts for timing,
  RCA, latency, tokens, remediation, and verification.

**Acceptance:** met by focused F.11 regression tests. The broader audit gaps
in P1–P4 remain recommendations and are not implemented in this phase.

### P1 — Bind results to the canonical matrix and controls

**Status: canonical scenario/control integrity implemented in F.12;
backward-compatible versioning in F.14; minimal run envelope implemented in
F.15.** Unversioned and version-1 results remain accepted. Version 2 records
an opaque run ID, declared mode, exact loaded matrix-byte SHA-256, and
timezone-aware run bounds. Case/control IDs remain bound to the canonical
matrix and optional record `run_id` values must match.

The mode and timestamps are caller claims; the digest only confirms that the
matrix loaded for calculation matches the digest string. It does not prove
that matrix was used during earlier execution or that the declared evaluation
mode is true. Code revision, execution environment, independent artifact
provenance, and evidence authenticity remain unresolved.

**Acceptance:** focused tests cover legacy/v1 compatibility, v2 synthetic and
declared-live-lab envelopes, unsupported or malformed metadata, digest
mismatch, run-bound timestamp ordering, record/run consistency, and unchanged
canonical label/control requirements. A declared live-lab mode is never
reported as independently verified execution.

### P2 — Report complete detection and classification accounting

**Status: classification accounting implemented in F.13.** Legacy scalar
definitions remain unchanged; new additive metrics report six-category
confusion and counts across cases and controls.

Remaining review: agree whether promotion decisions use the legacy
positive-only macro-F1 or additive six-category macro-F1. The additive metric
uses explicit `unknown` as a class, null as unclassified, and observed category
independently from detection. Controls are mandatory from F.12; their count is
not evidence of operational false-positive coverage without recorded,
reviewed live observations.

**Acceptance:** focused synthetic tests cover correct/incorrect/unknown/null
predictions, missed detection with retained classification, all six classes,
and undefined precision. No live accuracy is claimed.

### P3 — Add evidence, target, and response-safety measures

**Status: partial in F.13/F.14.** Target comparison and provenance coverage
are implemented only when explicit fields exist. Caller-reported
policy/approval state coverage is also implemented; runtime safety remains
not measured.

- The current matrix does not define target labels, so expected targets must
  be reviewed and explicitly added before target accuracy has a denominator.
- The optional provenance structure separates source sample timestamp, query
  evaluation timestamp, evaluator observation time, and evidence reference.
  It cannot establish timestamp truth, freshness, scrape age, or source health.
- Optional policy/approval fields validate the relationship between decision,
  approval, execution, verification, and timestamps, and report state and
  audit-reference coverage. They do not prove event authenticity, actual
  execution, or audit persistence. Missing fields remain excluded.

**Acceptance:** target accuracy is reported only for comparable explicit
labels; provenance has missing/incomplete coverage counts and does not claim
freshness; policy state coverage is reported without claiming actual safety.
Any future safety rate requires independently corroborated event evidence
and separately reviewed rules.

### P4 — Establish labeled anomaly and RCA evaluation sets

- Separate baseline fitting data from labeled holdout periods/services; record
  provenance and prevent train/test leakage.
- Measure anomaly false positives on healthy controls and recall on labeled
  events across documented operating conditions. Evaluate RCA with an
  independently reviewed rubric and provider availability separately.
- Review all thresholds and minimum sample counts with operators before
  setting acceptance criteria.

**Acceptance:** deterministic synthetic tests verify metric calculations;
separately approved labeled datasets include sample counts, provenance,
holdout boundaries, and reviewed labels. No threshold is adopted solely from
the current baseline or synthetic fixtures.

## Open operator decisions

- What is the intended definition of a positive detection: any alert, an
  incident record, or a category-correct alert?
- Must all five recommended controls be mandatory for a report, or should a
  run be marked partial with explicit control coverage?
- Should promotion decisions use the legacy positive-only macro-F1 or the
  additive six-category metric including healthy controls and `unknown`?
- What timestamp clock and skew policy applies to scenario injection,
  detection, controls, and recovery?
- Which source evidence and target identifiers may be retained in results,
  and what redaction/retention requirements apply?
- What minimum sample counts and quality thresholds are acceptable? No
  precision, recall, F1, latency, or RCA target is inferred here.
- Which live-lab scenarios and provider measurements, if any, will be
  separately authorized after the safety and operational prerequisites are
  reviewed?

## Phase F.16 readiness decision

| Dimension | Status | Evidence and remaining blockers |
| --- | --- | --- |
| Synthetic evaluation readiness | **READY** | F.27 adds a runnable fixture harness that classifies all 20 matrix IDs, compares explicit policy outcomes where a target exists, reports diagnosis fixture states, and writes raw schema-v2 results plus metrics. Its result is synthetic fixture evidence only; detection, integrations, execution, and recovery remain unmeasured. |
| Live-lab evaluation readiness | **BLOCKED** | No complete benchmark results or independent live evidence are present. Live scenarios require a new explicit authorization, lab health/context preflight, scenario-by-scenario approval, supported cleanup, and evidence review; the earlier pod-crash authorization is exhausted. See the [Controlled Live-Lab Evaluation Plan](./LIVE_LAB_EVALUATION_PLAN.md) for the required sign-off template; the plan itself is not authorization. |
| Measurement readiness | **BLOCKED** | Detection, timing, category metrics, control false-positive counts, remediation/verification rates, RCA JSON validity, and optional LLM numeric means can be calculated from supplied labels. Target accuracy has denominator zero because expected target labels are absent. Freshness and policy/approval safety are not measured; provenance and policy-state counts are schema coverage only. No accuracy or quality target is established. |
| Production readiness | **NOT ESTABLISHED** | Passing local tests validates code paths and metric arithmetic only. There is no completed labeled benchmark, authorized live-lab evidence, independently validated source provenance, operational acceptance threshold, or production safety evidence. |

### Verified facts

- The canonical file defines 20 unique incident case IDs, four per expected
  category, and five unique healthy-control IDs. The metric validator binds
  submitted result IDs/categories to the loaded matrix and requires exactly
  those controls.
- The matrix digest in version 2 is SHA-256 of the exact loaded bytes. It is
  matched during calculation; it cannot prove which bytes were used during
  historical execution.
- Unversioned and version-1 results remain accepted; version 2 requires a
  run envelope. The CLI turns validation errors into exit code 2 without
  presenting a traceback.
- The calculator reports legacy scalar metrics plus F.13/F.14 coverage
  outputs. Optional policy fields remain caller-recorded and policy safety is
  explicitly `not_measured`.
- The F.27 synthetic runner reports its generated fixture/classifier/policy
  outcomes separately from actual monitoring. Classification does not
  establish live detection accuracy; policy contract matches do not establish
  runtime policy safety.
- The recorded synthetic run on 2026-10-22 matched 20/20 category labels and
  16/16 applicable policy expectations; diagnosis fixtures were 18 available,
  one missing evidence, and one unavailable. Detection metrics were null and
  live remediation/recovery were not exercised.
- No live 20-case benchmark was run.

### Unverified assumptions

- A declared run ID, mode, timestamp, matrix digest, evidence reference, or
  audit reference is supplied honestly and has not been independently
  corroborated.
- A caller-recorded event timestamp corresponds to an actual source event.
- Synthetic fixtures reflect production telemetry, alert delivery, and
  Kubernetes behavior. Tests do not establish that correspondence.

### Next-milestone prerequisites

1. Resolve open operator decisions above, including target-label ownership,
   promotion metrics, clock/skew expectations, retention, and quality targets.
2. For live evaluation, obtain explicit authorization and define lab context,
   health gates, approved scenarios, stop conditions, cleanup verification,
   and evidence capture before running any scenario.
3. Preserve raw, appropriately redacted evidence and independently review
   run mode, matrix identity at execution, observed target IDs, and audit
   references. Do not treat the v2 envelope as attestation.
4. Report synthetic and separately authorized live-lab populations
   separately, with actual sample counts and limitations; make no production
   readiness claim based solely on the benchmark suite.
