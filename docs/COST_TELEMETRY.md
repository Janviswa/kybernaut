# Read-only Kubernetes cost telemetry

## Live four-service usage estimate

`GET /cost/usage` reads the existing fresh Prometheus process metrics for
`order-service`, `payment-service`, `user-service`, and `inventory-service`.
It reports current CPU cores used, process resident memory, request rate, and
p95 latency. It does not read or change Kubernetes resources.

This local-demo setup uses the illustrative internal USD rates below unless
overridden by environment variables:

- `KYBERNAUT_COST_CPU_PER_CORE_HOUR_USD`: USD 10.00 per CPU core-hour.
- `KYBERNAUT_COST_MEMORY_PER_GIB_HOUR_USD`: USD 5.25 per GiB-hour.

These defaults apply only when `KYBERNAUT_ENVIRONMENT=local-demo`. They are
illustrative owner-configured internal rates, not actual cloud-provider costs.
Other environments must explicitly configure both rates; the endpoint does
not assume local-demo pricing for production.

For each service, the estimate is:

```
hourly USD = current CPU cores * CPU USD/core-hour
           + current resident-memory GiB * memory USD/GiB-hour
monthly run-rate USD = hourly USD * 730
```

The monthly value is a point-in-time run-rate extrapolation, not a measured
monthly bill or actual cloud-provider cost. It excludes host overhead, other
workloads, and provider billing details. The all-service total is withheld if
any service has no fresh complete Prometheus sample. If either rate is missing,
usage remains visible and all prices are reported unavailable.

A single process-usage sample is not enough to recommend a safe reduction.
The page shows the largest current cost component and an illustrative
hypothetical 10% usage-reduction amount. This is a sensitivity scenario, not a
recommendation or guaranteed saving. Right-sizing requires historical peak
usage and the workload's configured requests/limits. The separate weekly report can
recommend cuts only when it was generated from real Prometheus range evidence
and non-example pricing.

The weekly cost report supports an explicit `--live-collect` mode. It is
separate from the timestamped JSON and synthetic inputs. Collection performs
only Prometheus HTTP `query_range` reads and `kubectl get` reads; it never
changes Kubernetes resources or triggers remediation.

## What live collection measures

- Kubernetes discovery is pinned to context `kind-kybernaut` and namespace
  `kybernaut-lab`. Only deployments in the remediation policy allowlist may be
  selected. The default is `demo-web`.
- The collector checks deployment selectors, requires the desired replicas to
  match the discovered non-terminating pod set, verifies each pod's
  Deployment-to-ReplicaSet ownership chain, requires every selected pod to be
  Ready, and requires positive CPU and memory requests for every container.
- PromQL expressions are mandatory. CPU output must be CPU cores and memory
  output must be bytes, with identity labels for namespace, pod, and container.
  Supply the label names explicitly; the collector does not assume metric
  names, label names, or label combinations. Live collection also requires an
  operator-reviewed query profile bound to the exact expressions and labels.
- Two additional PromQL expressions must return the raw source metric
  timestamps as Unix seconds with the same identity labels. Each source sample
  is checked against the existing Prometheus maximum-age and future-skew
  settings (`KYBERNAUT_PROMETHEUS_MAX_SAMPLE_AGE_SECONDS` and
  `KYBERNAUT_PROMETHEUS_MAX_SAMPLE_SKEW_SECONDS`). Stale or future-dated sample
  pairs are omitted and counted.
- A weekly sample is emitted only when every discovered container has both
  metric values at that evaluation timestamp. Missing samples remain gaps; they
  are not filled or treated as zero.
- CPU and memory utilization are total observed container usage divided by
  aggregate requests for the selected deployment. Values over 1.0 are retained
  to represent usage above requests.
- The recommendation is an aggregate request-sizing estimate based on p95
  utilization and the existing 60% target. It is not split into per-container
  requests and must be reviewed by the workload owner.
- The denominator is the request configuration discovered at collection time.
  Historical request changes are not reconstructed, so a request change during
  the analysis window can make earlier utilization ratios non-comparable.

Prometheus range results expose query evaluation timestamps. They do not prove
the age of the underlying scrape samples for arbitrary PromQL expressions.
The source-timestamp expressions must refer to the same raw metrics used by the
CPU/memory expressions. Review the monitoring configuration before relying on
recommendations. Reports retain identity labels, freshness thresholds, and
SHA-256 fingerprints of the four query expressions and the reviewed profile's
declared units/semantics; raw PromQL is not copied into the report. The profile
is a human-reviewed trust declaration, not a PromQL parser or proof that an
expression has the declared behavior.

## Operator-reviewed query profile

The `--query-profile` JSON file must use schema version 1 and contain exactly
these top-level sections: `schema_version`, `reviewed_by`, `reviewed_at`,
`identity_labels`, `query_sha256`, `cpu`, `memory`,
`cpu_source_timestamp`, and `memory_source_timestamp`.
`reviewed_at` must be a timezone-aware ISO timestamp that is not in the future.
`identity_labels` must name the same namespace, pod, and container labels passed
to the command. `query_sha256` must contain the lowercase SHA-256 digest of the
exact UTF-8 PromQL supplied for each of the four corresponding command-line
options (`cpu`, `memory`, `cpu_source_timestamp`, and
`memory_source_timestamp`).

Each metric section declares `source_metric` and `unit`. CPU must declare
`cores`, memory `bytes`, and both utilization sections must declare
`aggregation: per_container_usage`. Each timestamp section must declare
`unit: unix_seconds` and `semantics: raw_sample_timestamp`. The CPU timestamp
section's source metric must match the CPU section's; likewise for memory.
Metric identifiers and profile structure are checked before Kubernetes or
Prometheus collection, but metric names and semantic declarations must be
reviewed against the actual Prometheus configuration and expressions by the
operator. The collector does not infer or verify PromQL semantics.

## Pricing boundary

The existing pricing catalog describes instance types and their monthly prices.
Those prices are not automatically attributable to Kubernetes deployment
requests. Live Kubernetes request sizing therefore reports monetary savings as
`null` / “not estimated”; it never maps an instance price to a workload or
claims savings. A future Kubernetes cost estimate requires an explicitly
configured request-pricing model with declared currency, source, effective
date, and hourly CPU/memory rates. Provider and region are optional labels;
the model does not define a provider SKU or billing integration.

An optional JSON pricing file configures request-allocation estimates without
changing the existing VM catalog. See `config/pricing.kubernetes.example.json`;
its null rates are intentional placeholders, not prices. A usable operator
file must identify the `kubernetes_requests` model, currency, source, effective
date, and non-negative `cpu_per_core_hour` / `memory_per_gib_hour` rates.
Provider and region are optional labels. Optional
`workload_adjustments_per_hour` entries are fixed hourly allocation
adjustments by deployment.

For period duration `H`, aggregate current Deployment requests `C` cores and
`M` bytes, hourly rates `Pc` per core-hour and `Pm` per GiB-hour, and configured
workload adjustment `A` per hour:

```
CPU allocation estimate     = C * Pc * H
Memory allocation estimate  = (M / 2^30) * Pm * H
Adjustment estimate         = A * H
Current estimated cost      = CPU + memory + adjustment
Potential allocation saving = current estimated cost - proposed estimated cost
Savings percent             = saving / current estimated cost * 100, if current > 0
```

The report window hours are used; these are allocation estimates, not provider
invoice costs. An adjustment is held constant between current and proposed
estimates. Reports distinguish these estimates from monthly VM catalog savings
and realized savings. A pricing file is rejected for historical analysis if
its effective date is after the report period starts.

The checked-in Prometheus configuration currently scrapes the four application
service jobs; it does not configure Kubernetes container-resource metrics.
Live collection will only work after the operator has configured a suitable
Prometheus source and supplies valid queries and identity labels. No default
metric names or queries are provided. For safety, the CLI currently accepts
only a credential-free Prometheus URL on loopback; a remote production or
shared monitoring endpoint is not accepted.

## Explicit live command

Use the prior full week or provide a timezone-aware interval. Replace both
PromQL expressions and labels with values verified against the configured
Prometheus metrics; this example is intentionally not executable as written:

```sh
.venv/bin/python scripts/weekly_cost_report.py \
  --live-collect \
  --last-completed-week \
  --prometheus-url http://localhost:9090 \
  --cpu-query '<CPU cores grouped by namespace, pod, container>' \
  --memory-query '<memory bytes grouped by namespace, pod, container>' \
  --cpu-source-timestamp-query '<raw CPU sample timestamp grouped by identity>' \
  --memory-source-timestamp-query '<raw memory sample timestamp grouped by identity>' \
  --query-profile /path/to/operator-reviewed-query-profile.json \
  --kubernetes-pricing /path/to/approved-request-rates.json \
  --namespace-label '<namespace label>' \
  --pod-label '<pod label>' \
  --container-label '<container label>' \
  --workload demo-web
```

The command writes a unique JSON, Markdown, preview, and local delivery-ledger
entry under `reports/weekly-cost/`. Report retrieval remains read-only. Email
delivery continues to require the existing explicit opt-ins. Do not select a
workload outside the policy allowlist or substitute a different Kubernetes
context or namespace.

## Approval-gated request resizing

The optimizer and report generator are read-only. A resize is possible only for
a non-example, compatible Kubernetes pricing model and a reported
`request_downsize_candidate` that passes evidence coverage and peak-headroom
checks. The current executor is deliberately limited to homogeneous,
single-container-per-pod Deployments; multi-container, changing, or ambiguous
workloads are rejected rather than split by guesswork.

API flow (all calls require the existing operator JWT):

1. `POST /cost/resizes/preview` with `{"workload":"demo-web"}` returns a
   dry-run proposal and performs no Kubernetes call.
2. `POST /cost/resizes/requests` records a pending request and audit event.
3. A different operator with `owner` permission calls
   `POST /cost/resizes/{request_id}/approve` with a reason.
4. An operator with `remediation` permission explicitly calls
   `POST /cost/resizes/{request_id}/execute`.
5. `GET /cost/resizes/{request_id}` reads approval, execution, and verification
   state. The console displays the same status read-only.

Approval requests expire after 24 hours. No scheduled or automatic resize is
performed. Resize bounds can be set with
`KYBERNAUT_COST_RESIZE_MIN_CPU_CORES`,
`KYBERNAUT_COST_RESIZE_MAX_CPU_CORES`,
`KYBERNAUT_COST_RESIZE_MIN_MEMORY_BYTES`, and
`KYBERNAUT_COST_RESIZE_MAX_MEMORY_BYTES`. The kubectl command timeout defaults
to 120 seconds and can be set from 1 to 600 seconds with
`KYBERNAUT_COST_RESIZE_TIMEOUT_SECONDS`.

Execution re-reads the Deployment under the fixed cluster context and rejects
any changed UID, resource version, replica count, container identity, request,
or limit. It uses a JSON patch with resource-version and prior-value tests,
changes only CPU/memory requests, waits for rollout, and verifies the requests,
observed generation, and available replicas. It never changes limits, replica
counts, or workloads outside the existing lab allowlist. Failed verification
does not trigger an automatic rollback; the response and audit state instruct
the operator to inspect the Deployment. Execution does not record realized
savings.

Execution stages are persisted before the corresponding operation where
possible. A failure before patch submission is recorded as a preflight failure;
a definitive Kubernetes rejection is distinguished from a timed-out or
otherwise uncertain patch outcome. Uncertain outcomes are never retried
automatically. If the process stops while a request remains unresolved, an
`owner` may call `POST /cost/resizes/{request_id}/reconcile` after confirming
that the original execution process has stopped. Reconciliation performs only
a read of the allowlisted Deployment and persists an audit event. It can say
that current state matches the original plan or approved proposal, but it
cannot prove which actor or execution produced that state; proposed-state
matches therefore keep `executed` unknown and explicitly mark attribution as
unproven. Mismatches or read failures remain unresolved for manual review.
An interrupted reconciliation may be retried; newer attempt identifiers
prevent older read results from overwriting the newer result. Never reconcile
while the original execution process may still be active.

## Offline validation

Collector and report tests use mocked Kubernetes and Prometheus responses; they
do not contact a cluster or monitoring endpoint:

```sh
.venv/bin/python -m pytest tests/test_cost_telemetry.py tests/test_cost_optimizer.py tests/test_weekly_cost_report.py
```
