# CI Ownership and Python Support Decision

**Status: D-CI-01 through D-CI-07 are approved by the repository owner,
subject to their recorded conditions.** Approval records a policy decision;
it does not mean implementation, verification, or CI qualification is
complete. This record does not declare CI qualified or production ready.

## Scope and existing evidence

This decision concerns the authoritative GitHub Actions workflow, supported
Python versions, and the intended role of the other existing workflow. It
records related dependency, isolation, test-selection, and artifact questions
that must be resolved for a reliable qualification contract.

### Current workflow differences

| Workflow | Triggers | Python and dependencies | Checks | Artifacts |
|---|---|---|---|---|
| [`tests.yml`](../.github/workflows/tests.yml) | Push to `main`; pull requests targeting `main` | Ubuntu latest; host Python 3.11; the isolation harness builds its pinned test image and installs `requirements.txt` inside that image | Runs `scripts/ci_network_isolation.py`, which snapshots approved inputs, runs the full pytest suite in a Docker container with `--network none`, and emits JUnit and network-isolation evidence | Validates JUnit redaction and isolation evidence; conditionally uploads both files for seven days |
| [`ci.yml`](../.github/workflows/ci.yml) | Push to `main`; pull requests without a branch restriction | Ubuntu latest; host Python 3.12; the same harness builds the pinned test image and installs `requirements.txt` inside it. Host-side YAML validation separately installs `PyYAML==6.0.3`. | Runs the same isolated full-suite harness, validates JUnit redaction and isolation evidence, then parses selected YAML/configuration files. No separate benchmark rerun is present. | Validates JUnit redaction and isolation evidence; conditionally uploads both files for seven days |

The workflows overlap on pushes to `main` and pull requests targeting `main`.
`ci.yml` also runs for pull requests targeting other branches. The repository
currently has two full-suite runs with different Python minors; this is
configured behavior, not proof that both versions are supported or that either
workflow is a required branch-protection check.

### Python and dependency evidence

- D-CI-01 approves Python 3.12 as the initial qualification target. The
  Python 3.11 workflow is retained, but that configuration alone does not
  establish that Python 3.11 is a supported or qualified version.
- [`requirements.txt`](../requirements.txt) is a fully pinned pip-compiled
  lock whose header says it was generated with Python 3.11. That metadata does
  not prove compatibility with Python 3.12. Its checksum is pinned in the
  isolation harness; compatibility must be established by the approved clean
  Python 3.12 installation and test run. No dependency versions or approved
  hash are changed by this record.
- The test image installation is separate from host-side workflow tooling.
  `ci.yml` explicitly installs the locked PyYAML version for its later host
  YAML-validation step; the Docker test image independently installs the
  pinned requirements file.
- The token-issuer test remains in the unfiltered suite. Its code uses a
  temporary project tree and synthetic child-process environment; required
  Python 3.12 CI verification remains outstanding.
- The harness provides configured temporary/container state and disables
  test-container networking. The new child-process probe is part of the
  harness evidence contract, but no full harness or GitHub run has been
  executed as part of this phase.
- Both workflows validate redaction and isolation evidence before conditionally
  uploading JUnit and network-isolation reports with seven-day retention.
  Configuration does not establish that upload, redaction, or retention
  succeeded in an actual GitHub run.

These are descriptions of the current worktree configuration. The workflow
table does not decide which workflow is required, whether the workflows should
be consolidated, or what repository branch-protection settings are active.

No repository evidence was identified to propose a different Python minor
version beyond the two already selected in CI. Considering another version
would require separate compatibility evidence and owner approval; this record
does not add one as an equivalent option.

## Decisions and recorded conditions

The sections below preserve the original proposals and supporting context.
The approved status, scope, approver, date, and conditions for each decision
are recorded in the approval record. An approval does not satisfy its
implementation or verification conditions.

### D-CI-01 — Supported Python versions

**Approved policy:** Use Python 3.12 as the initial CI qualification target.
This does not establish compatibility or qualification until evidence is
collected.

| Alternative | Evidence and implications |
|---|---|
| Python 3.11 | Already selected by `tests.yml`; formal compatibility and successful qualification are not established by selection alone. |
| Python 3.12 | Already selected by `ci.yml`; formal compatibility and successful qualification are not established by selection alone. |
| Both Python 3.11 and 3.12 | Matches the two current selectors and gives a two-version qualification target, with additional maintenance and matrix runtime. Neither current workflow is evidence of a completed qualification run. |

No other version is proposed without repository support evidence.

- **Proposal status:** Approved subject to recorded conditions
- **Approval status:** `APPROVED`, subject to compatibility verification
- **Approved Python support claim:** Python 3.12 is the initial CI qualification target; compatibility and qualification evidence remain outstanding.
- **Approver, scope, and conditions:** See the per-decision D-CI-01 approval record below.

### D-CI-02 — Authoritative workflow and required check

**Approved policy:** Designate `ci.yml` as the authoritative workflow
candidate and retain `tests.yml` unchanged for now. This does not establish
GitHub branch-protection configuration or required-check enforcement; verify
those through repository settings before claiming enforcement.

| Alternative | Implications |
|---|---|
| Make `tests.yml` authoritative | The current 3.11 full-suite run becomes the required check; `ci.yml` would need an explicitly assigned complementary role or an approved retirement. |
| Make `ci.yml` authoritative | The current 3.12 isolated full-suite run and YAML validation become the required check; the duplicate full-suite run and `tests.yml` role would need an explicit disposition. |
| Retain both as complementary required checks | Requires a clearly separated purpose, support matrix, ownership, and branch-protection mapping so overlapping full-suite runs are intentional. |
| Retain one as non-required/diagnostic or retire it after an approved transition | Requires explicit ownership, transition conditions, and confirmation that required checks remain enforceable. |

Repository configuration does not establish branch-protection settings or
workflow ownership.

- **Proposal status:** Approved subject to recorded conditions
- **Approval status:** `APPROVED`, subject to recorded conditions
- **Approved workflow:** `.github/workflows/ci.yml` is the authoritative workflow candidate, not a verified required check.
- **Other workflow's approved role:** Retain `tests.yml` unchanged for now; its complementary, diagnostic, or retirement role remains undecided.
- **Branch-protection verification:** Not established by repository files; verify through repository settings before claiming enforcement.
- **Approver, scope, and conditions:** See the per-decision D-CI-02 approval record below.

### D-CI-03 — Dependency reproducibility and updates

**Approved policy, subject to recorded conditions:**

1. Use pip-tools 7.6.2 as the dependency-locking tool.
2. Preserve all 13 existing direct root dependency declarations in
   `requirements.in` without silently adding, removing, or changing any
   declaration.
3. Compile the root `requirements.txt` with fully pinned direct and transitive
   versions, preserving the documented `pip install -r requirements.txt`
   installation path.
4. Do not require package hashes in the initial implementation.
5. Use Python 3.12 as the initial CI qualification target, subject to a
   successful clean-environment compatibility run.
6. Use one compiled lock only if verification demonstrates compatibility across
   every approved Python/platform target. Otherwise, document and use separate
   locks as required by that matrix.
7. Keep compiler tooling separate from application dependencies, using
   `requirements-tools.txt` for the pinned pip-tools version.
8. Require reviewed source dependency changes, reproducible lock regeneration,
   and clean-install verification for dependency updates.
9. Authorize workflow cache-input changes only after the authoritative
   workflow and the other workflow's role have been decided. Once authorized,
   cache inputs should include the source, compiled requirements, and
   separately pinned compiler tooling inputs as appropriate.

Trade-offs: locking improves repeatability but adds a maintenance and review
process. Hash enforcement may require additional care for platform-specific
distributions. A successful dependency installation does not establish
application or test compatibility. Target-service requirement files and
dependency resolution remain independent; do not merge them into the root
input or lock.

- **Proposal status:** Approved subject to recorded conditions
- **Approval status:** `APPROVED`, subject to recorded conditions
- **Approved dependency policy:** Use pip-tools 7.6.2; preserve all 13 direct root declarations in the source input; compile a fully pinned root requirements file without mandatory hashes initially; keep target-service manifests separate; keep the Python 3.11 workflow unchanged; target Python 3.12 first.
- **Implementation and verification:** Source input and generated lock must be reviewed together; reproducible generation, clean Python 3.12 installation, and required tests remain outstanding. Compatibility failures must be resolved before CI qualification.
- **Python 3.11 compatibility:** Not established by the continued presence of its workflow.
- **Workflow cache changes:** Deferred until explicitly authorized.
- **Dependency-update owner and review procedure:** Not specified in the approval; resolve before defining the ongoing update process.
- **Approver and remaining conditions:** See the per-decision D-CI-03 approval record below.

### D-CI-04 — Test isolation and external service access

**Approved policy:** Require isolated writable test state and controls that
prevent or detect unintended external service access. The contract addresses
per-run database and filesystem paths, test credentials, and Prometheus,
Kubernetes, model-provider, target-service, and other network calls. Any
intentional local-service test needs an explicitly isolated service contract.

- **Proposal status:** Approved subject to recorded conditions
- **Approval status:** `APPROVED`, subject to recorded conditions
- **Approved isolation boundary:** Per-run writable state, explicit temporary database paths where applicable, controlled test environment, and test-specific isolation as detailed in the approval record.
- **Exceptions:** Intentional local-service or external-service tests require approved scope, isolated service contract, and documented controls.
- **Implementation and verification:** Audit the suite, verify database initialization cannot modify unintended persistent state, and verify tests do not depend on developer-specific environment settings. These conditions remain outstanding.
- **Approver and full conditions:** See the per-decision D-CI-04 approval record below.

### D-CI-05 — `test_token_issuer.py` selection and filesystem isolation

**Approved policy:** Retain `test_token_issuer.py` in the required,
unfiltered pytest suite, subject to CI verification. The inspected test code
copies the issuer script and writes synthetic `config/operators.json` under a
temporary directory, and invokes the copy with an explicitly constructed
synthetic environment. It does not write to or restore the repository's
`config/operators.json`. CI isolation still requires verification in the
approved Python 3.12 environment; approval does not establish that the test
has passed there.

- **Proposal status:** Approved subject to recorded conditions
- **Approval status:** `APPROVED`, subject to recorded conditions
- **Approved test selection:** Retain `tests/test_token_issuer.py` in the required, unfiltered pytest suite; do not invoke the file separately because both configured workflows already select it.
- **Required filesystem isolation and cleanup evidence:** Accept the existing temporary-directory approach for the copied issuer script and synthetic `config/operators.json`, subject to verification through the required CI test run. Verify isolation from repository operator configuration and developer-specific environment variables. No real operator credentials are required.
- **Decision and rationale:** Approved to preserve existing test intent and avoid unrelated changes; reassess before qualification if future implementation introduces repository-file writes or real credential dependencies.
- **Accountable approver identity:** Janviswa (GitHub identity)
- **Accountable approver role:** Repository owner
- **Auditable decision reference:** GitHub Issue #1, D-CI-05 owner decision
- **Decision date:** 2026-10-25
- **Conditions:** Verify the test passes in the approved Python 3.12 CI environment; verify isolation from repository operator configuration and developer-specific environment variables; reassess if implementation changes introduce repository-file writes or real credential dependencies. This approval does not establish CI qualification or production readiness.

### D-CI-06 — Test reports, artifacts, and retention

**Approved policy:** Publish the approved test report with the stated
retention and redaction controls; implementation remains subject to the
recorded conditions.

- **Proposal status:** Approved subject to recorded conditions
- **Approval status:** `APPROVED`, subject to recorded conditions
- **Approved report types and retention period:** Ordinary CI job logs and a JUnit XML test report; upload the JUnit report after successful and failed test runs only when the report exists and redaction validation passes; seven-day artifact retention.
- **Artifact owner and secret-redaction controls:** Repository owner is the accountable artifact owner. Validate handling with synthetic test data and confirm reports do not expose secrets, tokens, credential values, or environment dumps. Do not add verbose environment dumps, coverage HTML, source archives, or unrelated artifacts under this decision.
- **Accountable approver identity:** Janviswa (GitHub identity)
- **Accountable approver role:** Repository owner
- **Auditable decision reference:** GitHub Issue #1, D-CI-06 owner decision
- **Decision date:** 2026-10-25
- **Implementation status:** The current workflows generate JUnit and network-isolation reports, validate them, and conditionally configure seven-day artifact upload.
- **Qualification conditions:** Verify these steps in an approved clean-checkout workflow run, including the redaction behavior and actual artifact retention. If redaction validation fails, the affected report must not be uploaded. Configured workflow behavior is not evidence that upload or retention has succeeded. This approval does not establish CI qualification or production readiness.

### D-CI-07 — CI test network isolation

**APPROVED POLICY:** Use Option C, a layered policy, subject to the conditions
below. Mock-based isolation makes unit tests deterministic; it does not
guarantee that the runner denies network access. Enforced network isolation
requires a controlled execution environment whose policy applies to test
processes and their descendants. No claim is made that GitHub-hosted runners
provide a supported runner-level firewall mechanism for this repository.

| Option | Description and conditions |
|---|---|
| A — Mock-based unit-test isolation | Keep ordinary CI on GitHub-hosted runners. Require deterministic mocks for network-capable application boundaries, and isolate temporary files, databases, and subprocess fixtures. This improves test isolation but does not guarantee runner-level network denial or prevent arbitrary egress by a test or child process. |
| B — Enforced network isolation | Use a controlled execution environment with a policy that blocks unintended egress for the test process and descendants. Evaluate a self-hosted isolated runner or another demonstrably supported mechanism. Before claiming enforcement, require an observable denial probe that includes a child-process connection attempt to a synthetic local listener. Assign ownership for runner maintenance, security, and narrowly scoped exceptions. |
| C — Layered policy (preferred) | Use Option A for ordinary unit tests. Run security-sensitive or network-policy qualification tests in a separate controlled environment under Option B. Maintain a clear distinction between mock-based isolation and enforced network denial; passing mock-based tests alone is not evidence of egress enforcement. |

- **Proposal status:** Approved subject to recorded conditions
- **Approval status:** `APPROVED`, subject to recorded conditions
- **Approved policy:** Layered network isolation (Option C): ordinary CI tests use deterministic mocks, synthetic fixtures, and isolated test state for external-service boundaries; mock-based tests are not proof of runner-level network isolation; network isolation is separately verified in a controlled environment with enforceable egress restrictions covering test processes and descendants; intentional local-service tests use an explicitly scoped, isolated service contract and documented controls; Prometheus, Kubernetes, model-provider, and target-service integrations use mocks or synthetic fixtures by default; intentional external integration tests require explicit approval, documented destinations, and appropriate isolation controls; dependency installation is handled separately from test-stage network restrictions; ordinary test results remain separate from network-enforcement qualification evidence.
- **Harness execution mechanism:** The current test container is configured with Docker `--network none`; the harness also uses disposable bridge-network containers only for synthetic control connections. The actual workflow execution and probe result remain unverified.
- **Observable child-process probe and passing evidence:** A synthetic local-listener denial probe must demonstrate that the configured enforcement mechanism blocks the intended connection from a child process. Its reviewable evidence contract is implemented below, but no real Docker or GitHub Actions probe result has been produced.
- **Current worktree implementation:** `scripts/ci_network_isolation.py` runs a bounded child process from the actual pytest container against a synthetic listener bound to the Docker bridge gateway. A bridge-network control container must reach that listener; the pytest-container child must be denied; the listener records before and after the attempt are included in fail-closed evidence validated by `scripts/validate_network_isolation_evidence.py`. Schema-v3 evidence also requires the explicit expected revision, clean-worktree preflight, and cleanup outcomes. Focused tests cover the Docker-independent logic; the full harness has not been run, so this is implementation evidence, not a passing isolation or cleanup result.
- **Cleanup evidence:** Listener shutdown is checked after a bounded join. The harness records cleanup command outcomes and bounded removal inspections for the named peer and pytest containers and unique image. A failed or unverified cleanup blocks success; if the primary run fails before a complete bundle exists, cleanup failures are surfaced in the job log. This is not independent authentication or observed runtime evidence. See [`ci-network-isolation.md`](./ci-network-isolation.md) for the schema and limits.
- **Revision and worktree preflight:** The workflows pass `GITHUB_SHA` as the expected revision. The harness rejects a revision mismatch, tracked/untracked changes, and ignored Python sources in the recursively snapshotted source roots. This preflight is implemented but has not been exercised in an approved qualification run.
- **Remaining D-CI-07 conditions:** Controlled-runner ownership, security maintenance, allowed-destination and exception procedures, and review of real run evidence remain unresolved. No child-process probe result from a real Docker execution or GitHub Actions run is available.
- **Runner maintenance, security, and exception owners:** Must be documented before controlled-runner qualification; not yet assigned.
- **Accountable approver identity:** Janviswa (GitHub identity)
- **Accountable approver role:** Repository owner
- **Auditable decision reference:** GitHub Issue #1, D-CI-07 owner decision
- **Decision date:** 2026-10-25
- **Implementation status:** The harness now contains the synthetic child-process probe and evidence validator.
- **Qualification conditions:** Before claiming isolation or CI qualification, document and approve the controlled environment, enforcement mechanism, maintenance/security owner, allowed destinations, exception process, and probe acceptance criteria. A probe implementation or its unit tests do not demonstrate that isolation works. Require a passing controlled-environment execution with reviewable evidence; do not weaken security controls to make the probe pass. This approval does not establish CI qualification or production readiness.

## Qualification gate

CI must not be called qualified until the owner decisions above are recorded
and the selected required workflow has successful, reviewable runs for every
approved Python version. The qualification evidence should establish
reproducible dependency installation, an approved test selection, isolated
writable state, prevention or detection of unintended external access,
visible test/configuration failures, and any approved report/artifact retention
behavior. Mock-based isolation alone does not establish runner-level network
denial. Any claim of enforced egress isolation requires evidence from the
approved controlled environment, including the observable child-process
probe. Workflow consolidation or support claims must not be inferred from the
existence of the current YAML files.

CI qualification and production readiness remain **NO-GO** pending those
decisions and evidence.

## Approval record

The proposal rows below preserve the original recommendations; the approved
scope and conditions in each decision's rows are authoritative.

| Field | Record |
|---|---|
| Overall accountable approver identity | Per-decision approvers are recorded below; no separate overall approver field applies |
| Overall accountable approver role | Per-decision roles are recorded below; no separate overall approver field applies |
| Overall auditable decision reference | Each decision has its own reference below |
| Overall decision date | Each decision has its own date below |
| D-CI-01 proposal | Python 3.12 initial target — `PROPOSED` |
| D-CI-01 approval status | `APPROVED`, subject to compatibility verification |
| D-CI-01 accountable approver identity | Janviswa (GitHub identity) |
| D-CI-01 accountable approver role | Repository owner |
| D-CI-01 auditable decision reference | GitHub Issue #1, D-CI-01 owner decision |
| D-CI-01 decision date | 2026-10-25 |
| D-CI-01 approved scope | Python 3.12 is the initial CI qualification target |
| D-CI-01 conditions | Verify a clean dependency installation and required test suite under Python 3.12; do not remove or change the existing Python 3.11 workflow as part of this decision; report and resolve any compatibility failure before declaring CI qualification. This approval does not establish CI qualification or production readiness. |
| D-CI-02 proposal | `ci.yml` authoritative; leave `tests.yml` unchanged pending explicit role/retirement approval — `PROPOSED` |
| D-CI-02 approval status | `APPROVED`, subject to recorded conditions |
| D-CI-02 accountable approver identity | Janviswa (GitHub identity) |
| D-CI-02 accountable approver role | Repository owner |
| D-CI-02 auditable decision reference | GitHub Issue #1, D-CI-02 owner decision |
| D-CI-02 decision date | 2026-10-25 |
| D-CI-02 approved scope | Designate `.github/workflows/ci.yml` as the authoritative CI workflow candidate; retain `.github/workflows/tests.yml` unchanged for now. |
| D-CI-02 conditions | Do not claim GitHub branch protection or required-check enforcement is configured until verified through repository settings. Leave `tests.yml` unchanged until its complementary, diagnostic, or retirement role is explicitly decided. This approval does not establish branch-protection configuration, CI qualification, or production readiness. |
| D-CI-03 proposal | Evaluate pip-tools 7.6.2; preserve 13 root declarations in `requirements.in`; compile pinned root `requirements.txt` without mandatory hashes initially; qualify Python 3.12 first subject to approval; use one lock only if it covers all approved targets; separate tooling and target-service dependencies — `PROPOSED` |
| D-CI-03 approval status | `APPROVED`, subject to recorded conditions |
| D-CI-03 accountable approver identity | Janviswa (GitHub identity) |
| D-CI-03 accountable approver role | Repository owner |
| D-CI-03 auditable decision reference | GitHub Issue #1, D-CI-03 owner decision |
| D-CI-03 decision date | 2026-10-25 |
| D-CI-03 approved scope | Use pip-tools 7.6.2; preserve all 13 existing root dependency declarations in the source input; compile a fully pinned root requirements file from the reviewed source input; do not require package hashes initially; keep the four target-service dependency manifests separate from the root lock; keep the Python 3.11 workflow unchanged; use Python 3.12 as the initial CI qualification target under D-CI-01; defer CI cache-key changes until explicitly authorized. |
| D-CI-03 conditions | Review the source input and generated lock together; verify reproducible lock generation and a clean dependency installation under Python 3.12; run the required tests under Python 3.12; resolve compatibility failures before declaring CI qualified; do not claim Python 3.11 dependency compatibility solely because its workflow remains present. This decision does not establish CI qualification or production readiness. |
| D-CI-04 proposal | Require isolated writable state and external-access controls — `PROPOSED` |
| D-CI-04 approval status | `APPROVED`, subject to recorded conditions |
| D-CI-04 accountable approver identity | Janviswa (GitHub identity) |
| D-CI-04 accountable approver role | Repository owner |
| D-CI-04 auditable decision reference | GitHub Issue #1, D-CI-04 owner decision |
| D-CI-04 decision date | 2026-10-25 |
| D-CI-04 approved scope | Require isolated, per-run writable CI test state and a temporary database path configured through `KYBERNAUT_DATABASE_PATH` where applicable; prevent dependence on developer-specific environment settings or accidental modification of persistent shared state; default to synthetic fixtures, mocks, and in-process test clients for external service boundaries; isolate or clean persistent temporary locations within the test lifecycle; require an explicitly approved scope, isolated service contract, and documented controls for intentional local-service or external-network tests; do not represent mocking as runner-level network enforcement; retain token-issuer test selection and isolation as a D-CI-05 matter. |
| D-CI-04 conditions | Audit the existing test suite before implementation to identify tests needing isolation changes; verify that CI uses a controlled environment and database initialization cannot modify unintended persistent state; verify tests do not depend on developer-specific environment variables; preserve test intent and production behavior. This approval does not establish CI qualification or production readiness. |
| D-CI-05 proposal | Retain test in CI only after reliable filesystem isolation evidence — `PROPOSED` |
| D-CI-05 approval status | `APPROVED`, subject to recorded conditions |
| D-CI-05 accountable approver identity | Janviswa (GitHub identity) |
| D-CI-05 accountable approver role | Repository owner |
| D-CI-05 auditable decision reference | GitHub Issue #1, D-CI-05 owner decision |
| D-CI-05 decision date | 2026-10-25 |
| D-CI-05 approved scope | Retain `tests/test_token_issuer.py` in the required unfiltered pytest suite without a separate invocation; accept its temporary copied-script/synthetic-configuration approach subject to verification; use its synthetic child-process environment without real operator credentials. |
| D-CI-05 conditions | Verify the test passes in the approved Python 3.12 CI environment and remains isolated from repository operator configuration and developer-specific environment variables. Reassess if later changes introduce repository-file writes or real credential dependencies. This approval does not establish CI qualification or production readiness. |
| D-CI-06 proposal | Publish useful reports with explicit retention and secret redaction — `PROPOSED` |
| D-CI-06 approval status | `APPROVED`, subject to recorded conditions |
| D-CI-06 accountable approver identity | Janviswa (GitHub identity) |
| D-CI-06 accountable approver role | Repository owner |
| D-CI-06 auditable decision reference | GitHub Issue #1, D-CI-06 owner decision |
| D-CI-06 decision date | 2026-10-25 |
| D-CI-06 approved scope | Ordinary CI logs and JUnit XML; upload the report after successful or failed tests only if it exists and redaction validation passes; seven-day retention; repository owner is accountable for the artifact. Do not add verbose environment dumps, coverage HTML, source archives, or unrelated artifacts. |
| D-CI-06 conditions | Workflow report generation, validation, and conditional upload configuration are present; verify them in an approved clean-checkout run. Validate redaction with synthetic data, withhold an affected report if validation fails, and do not claim retention behavior is active until actual upload and retention are verified. This approval does not establish CI qualification or production readiness. |
| D-CI-07 proposal | Prefer layered network isolation (Option C); distinguish mocks from enforced egress denial — `PROPOSED` |
| D-CI-07 approval status | `APPROVED`, subject to recorded conditions |
| D-CI-07 accountable approver identity | Janviswa (GitHub identity) |
| D-CI-07 accountable approver role | Repository owner |
| D-CI-07 auditable decision reference | GitHub Issue #1, D-CI-07 owner decision |
| D-CI-07 decision date | 2026-10-25 |
| D-CI-07 approved scope | Layered network isolation: deterministic mocks, synthetic fixtures, and isolated state for ordinary CI; separately verified controlled-environment egress restrictions covering test processes and descendants; isolated service contracts for local services; explicit approval and documented destinations/controls for external integration tests; dependency installation separate from test-stage restrictions; ordinary test results separate from enforcement evidence. |
| D-CI-07 conditions | The harness probe implementation is present, but its implementation and unit tests do not demonstrate successful isolation. Before claiming enforcement, document and approve the controlled environment, mechanism, maintenance/security owner, allowed destinations, exception process, and probe acceptance criteria; then obtain a passing, reviewable child-process synthetic-listener result from that environment. Do not weaken controls to make it pass. This approval does not establish CI qualification or production readiness. |
| Rationale, scope, conditions, and exceptions | Recorded per decision above; completion of implementation and verification conditions remains outstanding. |
