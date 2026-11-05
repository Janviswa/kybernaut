# Kybernaut

**Agentic AIOps for Infrastructure Reliability & Cost Optimization**

Kybernaut is an infrastructure reliability project that monitors services, detects incidents, diagnoses failures, recommends recovery actions, and supports approval-gated remediation with verification and audit logging.

The goal is to build a safer, more reliable approach to infrastructure operations by combining monitoring, rule-based diagnosis, controlled recovery, and traceable remediation.

## Local End-to-End Demo

Kybernaut combines a telemetry anomaly policy, Prometheus/Kubernetes evidence
collection, rule-based incident classification, a deterministic remediation
policy, authenticated approvals, guarded execution, verification, and SQLite
audit/claim persistence. The intended lifecycle is:

**Synthetic monitor → active direction-aware MAD detection → synthetic
diagnosis and OOM evidence → deterministic incident classification and policy
→ distinct-operator approval → dry-run → honest verification status → SQLite
audit → cost recommendation.**

It reuses the active `DirectionAwareAnomalyPolicy`, authenticated operations
API, response graph, policy engine, dry-run executor, audit repository, and
cost optimizer. Monitoring, diagnosis, and Kubernetes evidence are deterministic
synthetic inputs; this simulation does not connect to Prometheus or Kubernetes.
It does not require Docker, cluster credentials, a `.env` file, or production
secrets. For the three required Python entry points and the safe test suite,
use the consolidated command block below.

From the repository root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m scripts.demo_incident
RUN_DIR="$(mktemp -d /tmp/kybernaut-eval.XXXXXX)"
.venv/bin/python scripts/run_synthetic_benchmark.py \
  --output "$RUN_DIR/synthetic-benchmark.json"
.venv/bin/python scripts/run_benchmark.py \
  --results "$RUN_DIR/synthetic-benchmark.json"
.venv/bin/python scripts/evaluate_anomaly_benchmark.py \
  --output "$RUN_DIR/direction-aware-anomaly-benchmark.json"
.venv/bin/python scripts/evaluate_anomaly_benchmark.py \
  --validate "$RUN_DIR/direction-aware-anomaly-benchmark.json"
.venv/bin/python -m pytest -q
```

The demo needs Python dependencies but no external services, credentials,
`.env`, Kubernetes context, or pre-existing database. Dependency installation
uses the configured package index; the demo, benchmarks, and tests run locally.
The demo creates a new SQLite file each time. Benchmarks write only to the
fresh temporary directory above. Keep the printed path to inspect results;
there is no reset step. Synthetic telemetry values and decisions are
deterministic; incident IDs, approval IDs, and audit timestamps
are generated per run and are not expected to match byte-for-byte.

`requirements.txt` lists the Python packages but does not lock exact versions.
The commands were exercised in the existing repository environment; a clean
clone and fresh dependency resolution were not tested.

The demo evaluates a deterministic synthetic memory time-series point against
the checked-in baseline using the active detector's 5.0 threshold. It then
passes the synthetic OOM evidence through the existing authenticated response
route, retrieves the `oom-killed-pod-restart` resolution from the OOMKilled
runbook, validates its evidence and two-ready-replica prerequisite, requires
the documented `remediation` permission and approval from a different
synthetic operator, and calls the existing dry-run executor. The demo patches
the namespace observer to synthetic
evidence during policy revalidation and guards the live remediation runner;
neither `kubectl` nor an external service is contacted. Each run gets an
isolated SQLite database under `/tmp/kybernaut-demo/`.

Runbook resolution uses the existing weighted local keyword retriever and a
strict YAML contract in selected runbooks. The workflow exposes the runbook
identifier, relevance/supporting evidence, incident applicability, required
permissions and prerequisites, allowed proposed action, expected verification,
risks, and approval requirement. Document text is untrusted: only validated
metadata can be considered, action selection still comes from deterministic
workflow rules, and every proposal is checked by the policy engine. The
retriever requires a relevance score of at least `0.20`, and the runbook's
declared evidence markers must match observed evidence (at least one listed
marker for that runbook). The authenticated operations API enforces the
`remediation` permission for both
request and approval decision. SQLite approval persistence rejects a requester
approving their own action; policy and runbook prerequisites are rechecked
against fresh state after approval. A missing/ambiguous match, missing
evidence, unmet prerequisite, or conflict with the deterministic plan produces
no action and is surfaced for human review. Runbooks cannot grant permissions,
disable approval, or authorize execution. The demo's dry-run does not verify
recovery.

The JSON summary reports the measured anomaly score and supporting values,
diagnosis availability, classification, policy checks, distinct approvers,
execution and verification status, an optimization recommendation based on
synthetic utilization and example prices, and whether API-visible audit events
match persisted SQLite rows. Root-cause analysis is explicitly reported as
unavailable because the external provider is intentionally not used.
Expected key fields include:

```json
{
  "success": true,
  "detection": {
    "detected": true,
    "metric": "memory_bytes",
    "deviation_score": 8.0,
    "threshold": 5.0
  },
  "classification": {"incident_type": "memory_leak"},
  "runbook_resolution": {
    "status": "applicable",
    "runbook_id": "oom-killed-pod-restart",
    "proposed_action": "restart_pod",
    "approval_required": true
  },
  "policy_decision": {
    "allowed": true,
    "requires_approval": true,
    "auto_execute": false
  },
  "approval": {"status": "approved"},
  "execution": {"mode": "dry_run", "executed": false, "ok": true},
  "verification": {
    "status": "not_run",
    "recovered": false
  },
  "optimization": {
    "status": "downsize_candidate",
    "pricing_is_example": true,
    "realized_savings": 0.0
  },
  "audit": {
    "event_count": 14,
    "persisted": true,
    "sqlite_path": "/tmp/kybernaut-demo/demo-<run-id>.sqlite3"
  },
  "live_runner_calls": 0
}
```

Verification is intentionally `not_run`: a dry-run executes no action and
cannot establish workload recovery. The optimization result is a potential
recommendation based on synthetic samples and example prices, not realized
savings. The audit record includes the detector evidence and workflow events;
the JSON lists event types and IDs.

The detector-miss path is covered by tests: a modeled anomaly below threshold
is reported as a miss, persisted, and does not create a downstream incident.
Policy denial, unavailable diagnosis, and audit-write failure are also tested.
This is a local simulation, not proof of live monitoring accuracy, production
readiness, or workload recovery.

## Synthetic Classification and Policy Benchmark

The quickstart command block above runs the canonical 20-case fixture
evaluation and independently recalculates its metrics. Its artifact contains
raw cases and controls, a version-2 run envelope and
scenario-matrix digest, provenance, computed metrics, and a summary. Run ID
and wall-clock timestamps vary; fixture outcomes and calculated metrics are
deterministic for the same matrix and code.

The harness calls the existing diagnosis function with a patched synthetic
Prometheus query source, then the classifier and deterministic policy engine
with in-process fixtures. It reports classification accuracy against the 20
canonical labels, policy decisions against explicit expected outcomes, and
diagnosis-fixture availability counts. Metric
definitions and limitations are in [docs/EVALUATION.md](docs/EVALUATION.md).

Detection is **not measured**: the harness does not observe alerts or run the
failure simulator. Live remediation, verification, and recovery are not
exercised. CPU classification uses a synthetic telemetry candidate; memory
classification uses OOM evidence rather than a measured growth trend. The
matrix's contextual live variants are not executed or enforced, so synthetic
scores do not establish live monitoring accuracy or production readiness.

Recorded synthetic fixture results on 2026-10-22:

- Classification: **20/20 (100%)** against synthetic matrix labels.
- Policy contract: **16/16 expected decisions matched**; 14 required
  approval, one was denied for missing deployment-state evidence, and one
  was an auto-eligibility probe using a marked synthetic policy profile.
  Four CPU cases had no actionable target and were skipped.
- Diagnosis fixtures: **18 available, 1 missing evidence, 1 unavailable**
  (18/20 available; synthetic mocked Prometheus results only).
- Detection: **not measured**. Remediation and recovery: **not exercised**.

## Synthetic Anomaly-Detection Evaluation

The quickstart runs the active anomaly policy and validates its generated
artifact. The runner uses `DirectionAwareAnomalyPolicy` from the active
`evaluate_service_anomaly` path, with its configured 5.0 MAD threshold and
current high/low direction rules. It reports point-level confusion counts
and metrics separately from actionable-candidate counts. The 2026-10-22
synthetic run evaluated **176 labeled points**: **TP 40, FP 4, TN 104, FN
28**, precision **0.9091**, recall **0.5882**, F1 **0.7143**, and false-positive
rate **0.0370**. Four modeled gradual CPU drifts were detected after a mean
**180 seconds** from the labeled drift onset.

These labels are synthetic or assumed: chronological baseline holdout and
bounded noise are assumed normal; injected deviations are positive by
construction. Eight missing or non-finite points are excluded as unavailable.
Available-disk-space detection is explicitly unsupported because that signal
is not among the active detector's features. These point-level results do not
measure incident-level detection or live monitoring performance. Further
assumptions and metric definitions are in
[docs/EVALUATION.md](docs/EVALUATION.md).

### Three-way synthetic comparison

To compare Isolation Forest, direction-aware MAD, and the experimental
Isolation Forest-or-MAD union on the same F.28 labeled synthetic observations,
write a separate versioned artifact and validate it:

```bash
.venv/bin/python scripts/evaluate_anomaly_benchmark.py --three-way
.venv/bin/python scripts/evaluate_anomaly_benchmark.py \
  --validate benchmark/results/three-way-anomaly-benchmark-v4.json
```

The command refuses to overwrite an existing output. All three methods use
the same per-service training split and eligible observations; missing and
invalid observations are excluded and counted for every method. Results
include aggregate and per-service confusion metrics and synthetic episode
delays. Labels are assumed or synthetic, not independently verified
operational ground truth; the comparison does not change the runtime detector
or establish production accuracy. Schema 4 records a canonical SHA-256
integrity digest and provenance metadata. The digest detects accidental or
uncoordinated edits; it does not authenticate authorship or replace rerunning
the benchmark against the frozen input. See
[docs/EVALUATION.md](docs/EVALUATION.md) for the comparison contract and
limitations.

### F.29 — False-negative analysis and frozen holdout

Reproduce the false-negative analysis without changing the F.28 artifact or
the runtime detector. Choose a new output path; the runner refuses to
overwrite an existing result:

```bash
RUN_DIR="$(mktemp -d /tmp/kybernaut-fn-analysis.XXXXXX)"
.venv/bin/python -m scripts.analyze_anomaly_false_negatives \
  --output "$RUN_DIR/f29-anomaly-false-negative-analysis.json"
```

F.28's 28 false
negatives recompute consistently: 24 are early points in four repeated CPU
drift traces (scores 0.75–4.5, below the 5.0 threshold), and 4 are low
request-rate steps (scores 4.26–4.32). Eight unavailable F.28 observations are
excluded, not counted as misses. The same unchanged threshold was evaluated
on separate deterministic episode families: 88 holdout points produced
TP 24, FP 0, TN 40, FN 24 (precision 1.0, recall 0.5, F1 0.6667, false-positive
rate 0.0). Misses persisted in the previously unseen gradual-memory-drift
family; concurrent latency/memory steps were detected. This is synthetic
evidence only, with assumed labels and a small scenario set. No threshold
candidate was selected or tested: the F.28 labels and repeated scenario
patterns do not justify changing runtime behavior without reviewed operational
labels and an accepted false-positive target. Details and per-scenario
metrics are in [docs/EVALUATION.md](docs/EVALUATION.md).

## Key Features

- **Incident Detection:** Detect unhealthy services using Prometheus metrics.
- **Incident Diagnosis:** Analyze configured service-health and application-performance signals.
- **Decision Engine:** Assign incident priorities and recommend investigation or recovery actions.
- **Approval-Gated Remediation:** Record an authorized human decision before execution, with requester/approver separation and fresh safety checks.
- **Restricted Recovery:** Use an allowlisted remediation helper to control which services can be restarted.
- **Health Verification:** Check service health before and after a restart.
- **Cooldown and Retry Limits:** Apply remediation guardrails to limit repeated attempts.
- **Incident Lifecycle:** Persist incidents and track their status in SQLite.
- **Audit Logging:** Record remediation outcomes in a persistent audit log.
- **Automated Tests:** Test remediation reservation, cooldown, retry limits, and outcome updates.

## Architecture

```text
Synthetic time series -> Direction-aware MAD detector -> Incident evidence
                                                       -> Diagnosis / RCA
                                                       -> Classification
                                                       -> Deterministic policy
                                                       -> Authenticated approval
                                                       -> Allowlisted action
                                                          (dry-run by default)
                                                       -> Verification
                                                       -> SQLite audit/claims

Synthetic utilization + pricing catalog -> Cost optimizer -> Potential savings
```

The deterministic policy engine is authoritative; RCA is advisory. Automatic
remediation defaults to disabled and action execution defaults to dry-run,
but environment overrides exist and must be checked for any real deployment.

### Weekly Cost Optimization Report

Run a reproducible local analysis with timestamped CPU/memory observations and
the existing cost optimizer. To use the previous complete Monday-to-Monday
week, with boundaries calculated in an explicit IANA timezone:

```bash
RUN_DIR="$(mktemp -d /tmp/kybernaut-weekly-cost.XXXXXX)"
PROJECT_ROOT="$(pwd)"
(cd "$PROJECT_ROOT/scripts" && "$PROJECT_ROOT/.venv/bin/python" weekly_cost_report.py \
  --synthetic \
  --last-completed-week \
  --timezone UTC \
  --sample-interval-minutes 60 \
  --out "$RUN_DIR")
```

`--last-completed-week` means local Monday 00:00 through the next Monday
00:00, end-exclusive; the report stores canonical UTC boundaries. The default
timezone is UTC. The same period can be supplied explicitly with
`--period-start` and `--period-end`.

The runner also accepts `--observations PATH` for JSON containing
`provenance`, optional `sample_interval_minutes`, and an `observations` array
with `timestamp`, `resource`, `service`, `instance_type`, `cpu_utilization`,
and `memory_utilization` (utilization is 0–1). A sample input can include
`importance: critical` to prevent a low-average downsize candidate from being
presented without owner review. The command produces JSON, Markdown, a
recipient-addressed email preview, and a SQLite run ledger in the chosen output
directory. Repeated previews are safe; the ledger suppresses a second email
delivery for a period already sending or sent.
For this fixture, the observed summary is 5 resources, 1 downsize candidate,
1 capacity-pressure resource, 2 incomplete/insufficient resources, $85.00
estimated potential monthly savings, and $0 realized savings. Email delivery
is `preview_only`; the recipient is listed only in the local preview artifact.

Analysis requires at least 24 observations per resource, 80% reporting-period
coverage, and a time-zone-aware timestamp on every sample. Missing observations
are not filled. Coverage below 80% and thin samples withhold recommendations.
Downsize candidates must meet both average CPU (<30%) and memory (<40%) limits,
must not have peaks over 80%, and are priced against p95 demand with the
existing optimizer. Critical resources require owner review. Capacity pressure
is flagged when at least 10% of CPU/memory observations reach 85%. Criteria are
configurable with `KYBERNAUT_COST_*` environment variables; the defaults and
example recipient are in [.env.example](./.env.example).
Only CPU and memory are currently supported. These heuristics are signals for
human review, not a claim that a resource is safe to resize.

The default recipient is `jananiviswa05@gmail.com`, configurable with
`KYBERNAUT_COST_REPORT_RECIPIENT`. Email is preview-only by default. Real SMTP
delivery requires both `--send-email` and
`KYBERNAUT_COST_REPORT_EMAIL_ENABLED=true`, plus SMTP host, port, sender,
username, and password environment settings. Credentials are read from the
environment or local `.env`; they are not in source or report content. Tests
use a mock SMTP sender. No real email was sent for this implementation.

#### Optional local cron schedule

The repository includes a cron fragment at
[`deploy/cron/kybernaut-weekly-cost.cron`](./deploy/cron/kybernaut-weekly-cost.cron).
It schedules a run every Monday at 09:00 UTC for the previous completed
Monday-to-Monday UTC period. It expects an external process to refresh the
timestamped observations file before the run; Kybernaut does not collect live
monitoring data. Replace the project, input, and report paths in the fragment.
The configured host must use a cron implementation that supports `CRON_TZ`.

To install, edit your own crontab and add the uncommented schedule line from
the fragment after replacing its paths. Nothing is installed or enabled by
this project:

```bash
crontab -e
```

Inspect the installed schedule with `crontab -l`. The report command and
period can also be run manually using the preceding example, or against a
supplied file:

```bash
PROJECT_ROOT="$(pwd)"
(cd "$PROJECT_ROOT/scripts" && "$PROJECT_ROOT/.venv/bin/python" weekly_cost_report.py \
  --observations /path/to/weekly-observations.json \
  --last-completed-week \
  --timezone UTC \
  --out /path/to/weekly-cost-reports)
```

Inspect recorded runs with Python's built-in SQLite module:

```bash
PROJECT_ROOT="$(pwd)"
"$PROJECT_ROOT/.venv/bin/python" -c \
  'import json, sqlite3, sys; db=sqlite3.connect(sys.argv[1]); print(json.dumps(db.execute("SELECT period_key, run_id, status, delivery_outcome, updated_at FROM weekly_cost_runs ORDER BY updated_at DESC").fetchall(), indent=2))' \
  /path/to/weekly-cost-reports/weekly-runs.sqlite3
```

Disable the schedule by running `crontab -e` and removing or commenting out
the Kybernaut entry. The cron fragment does not include `--send-email`, and
email remains disabled unless both that flag is added and
`KYBERNAUT_COST_REPORT_EMAIL_ENABLED=true` is explicitly configured alongside
SMTP settings. Keep report credentials in the local environment or `.env`,
never in the crontab.

The period-keyed SQLite ledger serializes delivery claims and suppresses a
second send while a run is `sending` or after it is delivered. A failed send
is recorded and can be retried on a later invocation. An interrupted run left
in `sending` is deliberately not auto-retried: inspect delivery/provider
records and the ledger before manually resolving it, since the process may
have stopped after the provider accepted the message. Preview runs are
recorded but do not prevent a later explicitly enabled send. Reports are
read-only recommendations: no infrastructure change is performed, estimated
savings use the labeled example pricing catalog, and realized savings remain
zero until a human separately approves and implements a change.

### What the local demo does and does not demonstrate

- **Exercised in the demo:** active direction-aware detector; authenticated API
  route; existing classifier, deterministic policy, approval and dry-run path;
  SQLite audit persistence; cost-optimizer calculation.
- **Synthetic/mocked:** telemetry point, service diagnosis, Kubernetes/OOM
  observation, approval operators, and utilization/pricing inputs. The demo
  patches revalidation to synthetic observations and makes no cluster or
  Prometheus calls.
- **Unavailable:** external RCA is reported unavailable. Dry-run verification
  is `not_run`; no workload recovery is claimed. Cost savings are potential
  and example-priced, not realized.
- **Evaluation:** the 20-case classification/policy benchmark and point-level
  anomaly benchmark use synthetic or assumed labels. Their scores do not
  represent live monitoring performance, incident-level accuracy, or
  production readiness. See [docs/EVALUATION.md](docs/EVALUATION.md).

## Technology Stack

| Component | Technology |
|---|---|
| API | Python, FastAPI |
| Validation | Pydantic |
| Monitoring | Prometheus |
| Service Isolation | Docker, Docker Compose |
| Incident and audit storage | SQLite |
| Payment history | PostgreSQL |
| Testing | pytest |
| HTTP Requests | HTTPX |
| Configuration | Environment variables and `.env` |

## Monitored Services

Kybernaut currently allowlists these target services:

- Order Service — port `8001`
- Payment Service — port `8002`
- User Service — port `8003`
- Inventory Service — port `8004`

The monitoring configuration is maintained in `monitoring/prometheus/prometheus.yml`.

The Payment Service stores its history in the `payment-postgres` PostgreSQL
container on a named volume. Set a strong, local-only `PAYMENT_DB_PASSWORD`
before starting Compose; the database port is bound to loopback only. Payment
requests are acknowledged only after PostgreSQL commits them, and `/health`
reports unavailable if the database cannot be reached.

The previous Payment Service kept records only in process memory. To preserve
those records during cutover, pause new payment writes, start the PostgreSQL
service, install `target_apps/payment_service/requirements.txt` in the local
Python environment used for migration. With payment writes paused and the old
service still running, use:

```bash
docker compose -f docker-compose.app-services.yml up -d payment-postgres
.venv/bin/python -m pip install -r target_apps/payment_service/requirements.txt
PAYMENT_DB_HOST=127.0.0.1 PAYMENT_DB_PORT=5433 \
  .venv/bin/python scripts/migrate_payment_history.py
docker compose -f docker-compose.app-services.yml up -d --build payment-service
```

The migration reads the local `.env` for its database password without
printing it. The import is idempotent by payment ID. Keep writes paused until
the new PostgreSQL-backed service is healthy; if writes occurred during the
copy, run the migration again before the old process is stopped.

For an active Prometheus `memory_leak` incident on `payment-service`, the
Console can run this cutover as a permission-checked, audited local-Compose
action when the API runs on the Docker host with
`KYBERNAUT_ENVIRONMENT=local-demo` and Docker Compose available to its process.
Before selecting **Execute**, pause all payment writes, including direct
callers, and confirm the pause in the prompt.
The action starts PostgreSQL, migrates and checks the existing records, builds
the updated image, and replaces only `payment-service`. It verifies health and
history but does not close the incident; fresh memory telemetry must confirm
recovery. If a step fails, the response names the failed stage; inspect the
service and database before retrying because earlier steps may have completed.

### Rotating local incident simulation

The local incident simulator is enabled in the `local-demo` environment when
`KYBERNAUT_FAILURE_SIMULATION_TOKEN` is configured; the simulator flag defaults
to enabled there and can be set to `false` to disable it. Production or other
environments remain disabled. Set the same non-empty token for the API and all
four target-service containers; Compose defaults the target services to
`local-demo` and passes the token through without supplying a built-in
credential. The Service Health page shows missing configuration without
revealing the token and reports whether the scheduler is running.

When enabled, the API immediately starts a batch. Each batch creates exactly
two incidents, one critical and one high, using a different service pair and
rotating failure type. A service does not repeat the same incident type the
next time it is selected. Each fault is bounded to 10 minutes. The next
automatic batch starts 10 minutes after both incidents in the current batch
are resolved. A user with the remediation permission can execute the
incident-specific clear action for one service; the incident is resolved only
after the target confirms the fault was cleared. Otherwise, the next cycle
waits for the batch to be resolved. From Incidents & Alerts, an authorized user
can also select **Simulate incidents now** to start a pair early. The simulator
is limited to these local target services and does not execute Kubernetes
remediation.

## API Endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/` | Project information and workflow |
| `GET` | `/health` | API health check |
| `GET` | `/incidents` | Detect unhealthy services from monitoring data |
| `GET` | `/incidents/history` | Read persisted incidents, optionally filtered by status |
| `GET` | `/metrics/incidents` | Read incident reliability metrics |
| `GET` | `/diagnose/{service}` | Diagnose a monitored service |
| `GET` | `/decide/{service}` | Generate a recommended action |
| `POST` | `/remediate/{service}` | Deprecated; returns HTTP 410 |
| `GET` | `/workflow/{service}` | Run advisory diagnosis, RCA, and policy evaluation; may persist supported telemetry incidents and handoffs |
| `POST` | `/workflow/{service}/respond` | Start an authenticated incident-response workflow; execution remains subject to policy and approval |
| `GET` | `/kubernetes/incidents` | Observe Kubernetes incidents and persist the completed observation |
| `GET` | `/monitor/status` | Read current application anomaly monitor status without polling |
| `GET` | `/local-service-simulation` | Read rotating incident simulator status and required configuration |
| `POST` | `/local-service-simulation/run-now` | Start the next two-incident pair early with remediation permission |
| `POST` | `/incidents/{incident_id}/execute-local-simulation` | Clear the selected local simulator fault with remediation permission |
| `GET` | `/approvals` | Read approval requests, optionally filtered by status |
| `GET` | `/approvals/{approval_id}` | Read one approval request |
| `POST` | `/approvals/{approval_id}/decision` | Record a decision; approval starts guarded execution when authorization and safety checks pass |
| `GET` | `/audit` | Read audit events, optionally filtered by incident |
| `GET` | `/demo/results` | Read isolated synthetic demo results |
| `GET` | `/demo/results/{incident_id}` | Read one isolated synthetic demo result |
| `GET` | `/incidents/{incident_id}/timeline` | Read audit events for an incident timeline |
| `GET` | `/cost/report` | Read the latest available cost report |

Interactive API documentation is available at `/docs` when the API is running.

### Remediation Safety

The legacy `POST /remediate/{service}` route is deprecated and returns HTTP
410 after validating the operator JWT and `remediation` permission. It never
invokes the legacy `sudo` helper. The former shared `X-Approval-Token` and
client-supplied `approved` flag are not accepted by that legacy route as
execution authorization. Current response and approval routes require an
authenticated operator with `remediation` permission; no shared approval
token is used. Requester/approver separation, persisted approval state, fresh
evidence, and backend policy checks remain enforced.

Migrate callers to `POST /workflow/{service}/respond` with an operator JWT
that has `remediation` permission and an `Idempotency-Key` header. That
workflow persists the action claim and approval request. A successful
`approved` decision starts execution in the same request; the backend still
checks the persisted approval, current policy, and fresh evidence.
The trusted `owner` permission allows the configured owner to approve their
own request. Any operator with `remediation` permission can approve their
own request through an explicit Execute action. Automatic remediation remains
disabled; execution still requires fresh backend policy and evidence checks.

The Kubernetes lab uses a limit of five actions per deployment in a rolling
hour (`KYBERNAUT_LAB_POLICY_MAX_ACTIONS` and
`KYBERNAUT_LAB_POLICY_WINDOW_SECONDS`). Other namespaces retain the general
`KYBERNAUT_POLICY_MAX_ACTIONS` and `KYBERNAUT_POLICY_WINDOW_SECONDS` limits.
The lab limit does not relax human approval, exact-target, or runbook readiness
checks; options that fail those checks remain unavailable.

## Project Structure

```text
kybernaut/
├── app/
│   ├── main.py
│   ├── database.py
│   └── services/
│       ├── decision_engine.py
│       ├── incident_detector.py
│       ├── incident_diagnoser.py
│       ├── incident_repository.py
│       ├── remediation_repository.py
│       └── remediation_service.py
├── config/
├── monitoring/
│   └── prometheus/
│       └── prometheus.yml
├── scripts/
├── target_apps/
│   ├── inventory_service/
│   ├── order_service/
│   ├── payment_service/
│   └── user_service/
├── tests/
│   └── test_remediation_repository.py
├── docker-compose.yml
├── requirements.txt
└── README.md
```

## Getting Started

For the no-infrastructure simulated workflow, use the [Local End-to-End
Demo](#local-end-to-end-demo) above. The remaining setup below is for running
the API and optional local observability/target services.

### Prerequisites

- Python and a compatible virtual environment
- Docker Engine and Docker Compose
- Git
- A configured Prometheus instance through the provided monitoring setup

### 1. Clone the repository

```bash
git clone https://github.com/Janviswa/kybernaut.git
cd kybernaut
```

### 2. Create and activate a virtual environment

On Ubuntu:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
python -m pip install -r requirements.txt
```

### 4. Configure the environment

Create a local `.env` file from the checked-in example and replace the
placeholder secrets:

```bash
cp .env.example .env
nano .env
```

Configure `KYBERNAUT_JWT_SECRET` with a strong, unique local secret. Response
and approval actions rely on authenticated operator permissions and persisted
approval controls; no separate shared approval token is configured. Set
`GRAFANA_ADMIN_PASSWORD` to a strong,
unique Grafana admin password before starting Compose. Generate secrets with
a trusted password manager or run `openssl rand -hex 32` locally, then enter
them in `.env`; do not put real secrets in tracked files or share them in logs.

Keep `.env` private and never commit it.

### Operator login and password provisioning

The UI signs in with `POST /auth/login` using an operator username and
password. The server verifies credentials against two separate files:
`config/operators.json` holds operator IDs and their explicit permissions;
`config/operator_password_hashes.json` holds Argon2id password verifiers only.
Both files are local/secret configuration and are ignored by Git. The verifier
file is created with owner-only permissions. Do not put plaintext passwords in
either file, in `.env`, or in command-line arguments.

To bootstrap an account, an administrator must first create or update
`config/operators.json` with the intended operator ID and least-privilege
permissions, then provision its password through the hidden interactive
prompt:

```bash
.venv/bin/python scripts/provision_operator_password.py --operator-id <configured-id>
```

The password must be at least 12 characters. To intentionally reset an
existing verifier, add `--reset`. The command does not create operator
accounts or grant permissions. For deployment, mount the operator
configuration and password-verifier file from the approved secret/config
management system; `KYBERNAUT_OPERATORS_FILE` and
`KYBERNAUT_OPERATOR_PASSWORD_FILE` may point to those mounted files. The
password-verifier file must be readable by the API process and, on POSIX
systems, must not grant group or other access.

The login response contains a bearer access token with the existing
`sub`, `iss`, `aud`, `iat`, `exp`, and `permissions` claims. Tokens expire
after 15 minutes; the existing issuer/audience/signature/expiry validation
continues to apply to all protected routes. The UI keeps the token in memory
only. Logging out clears that browser session; it does not revoke a token
already issued, which remains valid until expiration. Remediation permission
remains separate from `read`, and the approval workflow plus separate
remediation-token checks remain required.

The `scripts/issue_operator_token.py` command remains available for
development and automated tests that need a short-lived JWT. Ordinary UI
users should use the username/password login instead.

The optional Prometheus feature freshness settings are
`KYBERNAUT_PROMETHEUS_MAX_SAMPLE_AGE_SECONDS` (default `30`) and
`KYBERNAUT_PROMETHEUS_MAX_SAMPLE_SKEW_SECONDS` (default `10`). They apply to
the host-run API process, not to Prometheus itself.

The API also starts an application anomaly monitor in its lifespan. It
evaluates the four configured application services sequentially every 60
seconds by default (`KYBERNAUT_ANOMALY_POLL_INTERVAL_SECONDS`, minimum 10
seconds). Set `KYBERNAUT_ANOMALY_MONITOR_ENABLED=false` to disable it. A
process-wide file lock elects one polling process per checkout on the host;
other workers wait and retry leadership, so reloads or multi-worker servers do
not run concurrent pollers. Startup begins after database and anomaly-policy
initialization; application shutdown cancels polling and releases the lock.
Only complete, fresh detector results may create/update CPU or memory
anomalies. Healthy complete results resolve those active anomaly records;
unavailable or failed telemetry neither creates nor resolves them. Records use
the normal incident repository and appear in `/incidents/history`. Each record
retains a structured snapshot of its source metric and timestamps. The monitor
also runs diagnosis for active anomaly incidents, persists incident state, and
dispatches authorized handoffs. Diagnosis results, runbook matches, limitations,
and audit references are included in the read-authorized incident-history
response. Repeated polls with unchanged metric evidence do not repeat a
completed diagnosis; failed attempts retry with bounded backoff. Operators may
also initiate workflows through the existing authenticated APIs; neither
trigger replaces the other.

`GET /monitor/status` requires an authenticated operator with `read`
permission. It is a read-only snapshot and never starts polling or performs
operational actions. `configured_enabled` reports configuration (`true`,
`false`, or `null` before lifespan initialization). `lifecycle_state` is
`not_started`, `running`, `stopped`, or `disabled`; `poll_activity` is `idle`
or `in_progress`. `leadership_state` distinguishes `unknown`,
`not_applicable`, `leader`, `follower`, `lock_unavailable`, and `not_leader`.
The endpoint describes only this process, not cluster-wide monitor health.

`last_poll_attempt_at` is the cycle start; `last_completed_poll_at` and
`last_outcome` describe the most recently completed full cycle. An active
cycle is represented separately by `poll_activity`; it does not replace the
previous completed outcome. `health` is `disabled`, `unknown`, `healthy`, or
`degraded`: it is `healthy` only when the process is running, no cycle is in
progress, the last completed cycle succeeded, and overall and per-service
freshness are fresh. While polling, health is `unknown` unless the previous
completed result was successful and neither it nor any service is stale;
health is `degraded` if the previous completed outcome was not successful,
any completed result is stale, or a service completed in the current cycle
with a non-`evaluated` result. A stopped or not-yet-started monitor is
`unknown`, not healthy.

Overall `freshness` / `status_stale` is based on age of
`last_completed_poll_at`, not the latest attempt. Each service reports its
last completed `outcome` (`unknown`, `evaluated`, `unavailable`, or `error`),
`poll_activity`, attempt/completion/success timestamps, and nullable
`status_stale`; per-service staleness is based on `last_completed_at`.
Freshness is `unknown` (`status_stale: null`) without a valid completion
timestamp, `fresh` (`false`) through exactly two configured polling intervals,
and `stale` (`true`) only when age is strictly greater than two intervals.
Future timestamps are treated as unknown freshness.

The aggregate `last_outcome` is `unknown` before a completed cycle, `success`
when every configured service was evaluated, `error` when all failed with an
exception, `unavailable` when all evaluations were unavailable, and `partial`
for mixed or skipped service results. `health` is degraded for a failed,
partial, unavailable, or stale completed result, and healthy only under the
conditions above. `/health` remains an API liveness check and does not
establish monitor health. Status is not proof of production reliability.

`health` and `last_outcome` describe polling and anomaly evaluation;
`workflow_health` separately summarizes the latest downstream component
outcomes. Workflow health is `disabled` when monitoring is disabled and
`unknown` when it is not running. While running, failed, retrying, unavailable,
or stale successful component outcomes yield `degraded`; pending, unknown, or
successful outcomes without a completion timestamp yield `unknown`. It is
`healthy` only when every applicable component is successful and fresh or
explicitly `not_applicable`. Component freshness follows the same threshold
as poll freshness: results stay fresh through exactly two configured poll
intervals and become stale only when older. Each component reports its outcome
separately from `freshness` / nullable `status_stale`; top-level component
freshness is stale if any service is stale and fresh only when all services
are fresh. `last_successful_workflow_at` is historical and does not itself
mean the monitor is currently running or its latest outcomes remain fresh.
These workflow summaries are process-local and are not cluster-wide health
or proof of production reliability.

Kubernetes context is gathered only when an explicit one-to-one mapping exists
in `config/application_workload_map.json`. The checked-in mapping is empty until
operators configure trusted application-service to deployment identities.
Missing, invalid, or ambiguous mappings are recorded as diagnostic limitations,
and no target-specific Kubernetes evidence is associated. Automatic diagnosis
does not submit actions or create approvals, and never invokes the remediation
executor, even if its global mode is configured as live. Approval and response
remain behind their existing authenticated APIs. This is application anomaly
polling, not host-level Node Exporter monitoring.

### 5. Observability stack configuration

The main `docker-compose.yml` describes the target services, Prometheus,
Grafana, Loki, and an Alloy syslog receiver. Target containers send logs to
Alloy with fixed service tags; the collector does not need Docker-daemon
socket access. Do not reconcile that full project against an existing
Kybernaut runtime without reviewing the live container differences first.

`docker-compose.observability.yml` is a separate, isolated Loki and Grafana
Alloy configuration. It uses a dedicated network and Loki volume, publishes
Alloy's unauthenticated TCP syslog receiver only on host loopback port 1515,
and does not publish Loki or mount the Docker socket or Docker data directory.
Other containers deliberately attached to Alloy's dedicated network can still
reach its unauthenticated receiver and Loki.
This design does not expose the Docker-daemon socket; mounting that socket
read-only would not make Docker API requests read-only.

The active isolated observability Compose stack checks Loki `/ready` and
Alloy `/-/ready`, and waits for Loki to be healthy before starting Alloy.
Docker health checks report unhealthy state but do not restart a process that
remains running; `unless-stopped` applies when a container exits.
Recovery from a persistent unhealthy state requires operator diagnosis and a
targeted recovery action.
The app-only Compose overlay does not manage Alloy and assumes the full-stack
listener is already available. Legacy files under `monitoring/promtail/` are
retained but are not used by either active Compose configuration.

The four application containers have previously been migrated individually
to Docker syslog logging and validated with service-specific count-only Loki
queries. Those runtime checks do not validate this updated Compose
configuration, which has not been deployed as part of this change. The
Alloy write-ahead log is disabled. Docker non-blocking delivery uses a bounded
buffer and may drop messages when that buffer fills; delivery is not lossless.

Pinned Loki and Alloy configurations were validated, and single-message
canaries confirmed ingestion. This does not establish sustained-load
reliability, delivery through collector outages, or lossless buffering.
Seven-day retention (`168h`) is configured, but actual deletion behavior is
unverified. Docker's health state and readiness probes do not guarantee
continuous log delivery or automatic recovery from a running-but-unhealthy
collector.

See [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md) for read-only operational
checks, count-only queries, rollback backup names, migration evidence, and
remaining limitations.

The separate, full-stack `docker-compose.yml` requires
`GRAFANA_ADMIN_PASSWORD`; Compose rejects it when unset or blank. That value
is only the initial Grafana admin password. An already-initialized Grafana
data volume retains its stored credentials when the environment value changes.

For read-only Compose configuration inspection, use only the isolated file:

```bash
docker compose -p kybernaut-observability \
  -f docker-compose.observability.yml config --quiet
docker compose -p kybernaut-observability \
  -f docker-compose.observability.yml ps
```

Do not reconcile the full-stack configuration with the currently running
services. Any future Grafana network or Loki datasource change, or any
application rollback, requires separate review and approval. Preserve the
existing Prometheus anonymous volume, Grafana data volume, and Portainer data.

### 6. Start the Kybernaut API on the host

Keep the API outside Compose. From the project root with the virtual
environment activated, and with the required local `.env` values configured:

```bash
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The API's Prometheus clients use `http://localhost:9090`; the Compose
configuration publishes Prometheus on that host port. Open the API
documentation at `http://localhost:8000/docs`.

### React Console development

Run the development API and Vite frontend in separate terminals. From the
project root, start the API with reload enabled:

```bash
KYBERNAUT_REMEDIATION_MODE=live .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8010 --reload
```

This developer launch enables live remediation only when the current
`kubectl` context is exactly `kind-kybernaut`; the backend still enforces
policy, approval, fresh-evidence checks, and recovery verification. Other
launches retain the `dry_run` default.

Then start the frontend:

```bash
cd UI
npm run dev -- --host 127.0.0.1 --port 5173
```

Vite forwards `/api` requests to the development API on port `8010` and
removes the `/api` prefix. This development frontend does not use or change
the host API on port `8000`. If an existing non-reloading API process has
stale routes, stop that local process and start the command above.

### Operations Console

The API serves a lightweight, same-origin read-only console at
`http://127.0.0.1:8000/console/`; no separate frontend server, Node runtime,
or JavaScript package installation is required. Start the API as above, then
issue a short-lived token for the configured `read-only` operator:

```bash
cd scripts
../.venv/bin/python issue_operator_token.py --operator-id read-only --minutes 30
```

Paste the token into the Operator session field. It is held only in page
memory, sent as an Authorization header to authenticated read endpoints, and
cleared on disconnect or page reload. Do not paste it into URLs, screenshots,
or shared browser consoles. The frontend uses the same origin as the API; set
the backend bind address/port in its Uvicorn command when a different local
URL is needed. Weekly report lookup defaults to `reports/weekly-cost`; set
`KYBERNAUT_WEEKLY_COST_REPORT_DIR` in the backend environment to use another
directory.

The console reads `/health`, persisted `/incidents/history`, `/approvals`,
`/audit`, incident timelines, and `/cost/report`. Its API health indicator is
real API health; its incident and audit panels show stored records, not live
monitoring. The UI deliberately does not call the Prometheus incident scan or
Kubernetes observer. Telemetry/anomaly detection is therefore shown as not
queried, not inferred from an empty history. Cost data and its preview/sent
status come from the latest generated weekly report and its SQLite ledger;
synthetic provenance and example pricing are displayed as provided. Approval
requests can be inspected but not approved/rejected from this console. No
workflow-start or remediation control is exposed.

Synthetic demo records are separate from application history. Run
`.venv/bin/python scripts/demo_incident.py` to create a local dry-run demo in
the system temporary directory (`kybernaut-demo`); after authenticating with a
read-authorized operator, the console's **Synthetic demo results** panel reads
the incident, approval record, and audit events from that isolated database.
The `/demo/results` API is read-only and uses a fixed server-side directory;
the client cannot choose a database path. Older demo databases without an
incident-history table remain viewable from their audit and approval records.
Malformed or inaccessible matching databases are omitted and counted in the
console warning rather than hiding other readable demo results.
The console marks this data synthetic, and demo records are not added to the
normal incident database or eligible for approval or remediation actions.

### Run the automated tests

```bash
.venv/bin/python -m pytest -q
```

The token-issuer tests use a temporary project tree with a copied script and
synthetic operator configuration; they do not write to the repository's
`config/operators.json`. Both configured workflows include them through
unfiltered pytest. Their required CI verification remains outstanding. To run
focused demo and benchmark tests:

```bash
.venv/bin/python -m pytest -q \
  tests/test_demo_incident.py tests/test_synthetic_benchmark.py \
  tests/test_anomaly_benchmark.py \
  tests/test_anomaly_false_negative_analysis.py \
  tests/test_benchmark_metrics.py
```

Historical validation reported on 2026-10-22: the focused demo/benchmark
selection **156 passed** and the broad suite **556 passed** with
`tests/test_token_issuer.py` excluded. These are not results for the current
full-suite command. Rerun the commands to verify the current checkout;
recorded results are evidence from the listed run, not a guarantee for every
environment or CI qualification.

To inspect persisted demo audit rows, replace the sample `DB_PATH` below with
the exact `sqlite_path` printed by your demo run, then run:

```bash
DB_PATH=/tmp/kybernaut-demo/demo-<run-id>.sqlite3
.venv/bin/python -c 'import sqlite3,sys; db=sqlite3.connect(sys.argv[1]); print(*db.execute("SELECT event_type, outcome FROM audit_events ORDER BY id"), sep="\n")' "$DB_PATH"
```

## Safety Design

Kybernaut incorporates several controls around remediation:

- Service allowlisting prevents arbitrary service names from being accepted.
- Approval requires an authorized human decision; requester separation and fresh backend safety checks remain required to execute.
- The request must explicitly set `approved` to `true`.
- The remediation service validates the active incident and checks current health.
- Cooldown and retry limits restrict repeated remediation attempts.
- A restricted helper controls the restart operation.
- Health checks and audit records support post-action verification and traceability.

These controls reduce operational risk but do not replace production security reviews, least-privilege configuration, monitoring, and failure testing.

## Current Status

The current implementation includes request-driven FastAPI workflows,
Prometheus-based incident detection, Kubernetes observation, rule-based
classification and policy, SQLite persistence, approval-gated remediation,
and audit records. The local demo runs the active anomaly policy with
synthetic telemetry and drives a synthetic incident through the authenticated,
approval-gated dry-run path; it does not prove live integrations or production
readiness.

### Disposable SQLite recovery toolkit

The explicit backup, validation, and restore utility is documented in
[docs/DATABASE_RECOVERY_TOOLKIT.md](docs/DATABASE_RECOVERY_TOOLKIT.md). It is
restricted to disposable paths beneath the system temporary directory and is
not a production backup system. It does not configure scheduled backups,
select production destinations, or establish recovery objectives.

### Demo and validation limitations

- The demo uses synthetic Prometheus-style diagnosis and Kubernetes
  observations. It does not contact Prometheus, Kubernetes, Groq, or target
  services. RCA is intentionally unavailable in this demo.
- The demo verifies that policy allows a proposal, approval is required and
  recorded, the approved action completes in dry-run mode, and audit rows
  exist. It correctly reports that recovery verification was not run because
  no action executed.
- The actual Prometheus/Kubernetes integrations remain separate runtime
  paths that require their configured services and credentials. The demo
  does not validate those integrations.
- SQLite is the implemented local database. The repository does not
  establish production topology, backup/restore objectives, audit retention,
  or production readiness. See the
  [deployment architecture decision](docs/DEPLOYMENT_ARCHITECTURE_DECISION.md)
  and [evaluation limitations](docs/EVALUATION.md).

See [docs/EVALUATION.md](docs/EVALUATION.md) for the synthetic evaluation
protocol, benchmark result schema, healthy controls, and live-validation
limitations. Scenario definitions and synthetic tests are not evidence that
the 20-scenario benchmark has been executed.

### Historical Validation Results

The latest reported full test-suite result before the local observability
stack changes was **311 passed**; it is historical for this change. An older
development report recorded 25 tests passing. Neither count is a 20-scenario
benchmark result.

The following development-environment results are historical and are not
claims of current integration or production validation:

- **Incident detection:** Verified a controlled `user-service` outage was detected as a critical incident.
- **Recovery detection:** Confirmed the service returned to a healthy state and the incidents endpoint reported zero active incidents.
- **Incident validation:** A request with an invalid incident ID was skipped without restarting a healthy service.
- **End-to-end remediation:** An approved request for a detected outage restarted `user-service` and returned `status: verified`, with `health_before: false` and `health_after: true`.
- **Audit persistence:** The successful remediation outcome was verified in both SQLite and the JSONL audit log.

The 20-scenario benchmark has not been shown to have run. Pod-crash code
validation is complete; live validation is pending after a prior authorized
attempt failed to produce fresh crash evidence. That authorization is
exhausted. Broader failure-injection, concurrency, deployment, and production
security testing remain necessary.

## Roadmap

- [ ] Expand automated unit and integration tests. Local tests cover
  implementation paths using synthetic inputs, mocks, and temporary
  databases; the roadmap does not define a completion threshold. Local test
  runs are not clean-checkout CI qualification.
- [x] Configure GitHub Actions workflows for automated testing. Python 3.11
  and 3.12 workflow files exist; their presence and local pytest runs do not
  establish successful clean-checkout CI qualification. D-CI-01 through
  D-CI-07 are approved by the repository owner subject to recorded
  implementation and verification conditions in
  [CI_OWNERSHIP_DECISION.md](docs/CI_OWNERSHIP_DECISION.md); those conditions
  are not all complete.
  **CI qualification: NO-GO.**
- [ ] Improve incident correlation and diagnosis. Deterministic diagnosis and
  classification code and synthetic/mock-based tests exist, but diagnosis
  evidence freshness handling and live behavior are not established; see the
  [repository completion audit](docs/REPOSITORY_COMPLETION_AUDIT.md).
- [ ] Strengthen remediation failure and recovery handling. Failure,
  approval-gating, and verification paths have local test coverage; safe
  reconciliation of partial states and deployment-level guarantees remain
  outstanding, as described in the
  [repository completion audit](docs/REPOSITORY_COMPLETION_AUDIT.md).
- [x] Develop cost-aware infrastructure recommendations (offline estimates
  are implemented and fixture-tested; operational inputs and deployment are
  not qualified)
- [x] Explore LLM-assisted diagnosis with explicit safety boundaries
  (advisory integration and mock-based tests exist; live provider behavior
  and quality are unverified)
- [x] Provision local observability dashboard configuration (configuration
  has static test coverage; runtime integration and resilience are not
  independently verified).

Milestone 2 in the
[repository completion audit](docs/REPOSITORY_COMPLETION_AUDIT.md) also leaves
the legacy `/remediate` capacity-ledger boundary unresolved. Do not infer that
the response-graph capacity controls cover that separate path. Production
readiness is not established.

## Author

**Janani V. — Janviswa**

GitHub: https://github.com/Janviswa

## License

No license has been selected yet. Until one is added, the repository should not be treated as granting general permission to reuse, modify, or redistribute the code.# kybernaut
Agentic AIOps for Infrastructure Reliability and Cost Optimization
