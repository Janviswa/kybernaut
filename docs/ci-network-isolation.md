# CI pytest network isolation

Both GitHub Actions test workflows build the test environment before starting
pytest. The build installs the pinned `requirements.txt` and Git from a pinned
Debian snapshot while Docker networking is enabled. The complete pytest process
tree then runs in a disposable container with Docker `--network none`; there is
no host-network or Docker-socket mount. Docker capability is mandatory: the
runner fails closed if it cannot build or inspect the containers.

The test container uses a read-only root filesystem and a read-only `/src`
snapshot. Its only writable mounts are a `/tmp` tmpfs for test state and the
JUnit report directory. The isolation evidence is not mounted into the test
container. Pytest runs as the non-root workflow runner UID,
with all Linux capabilities dropped and `no-new-privileges` enabled. The
snapshot omits original dotenv files, local operator configuration, virtual
environments, database state, and Git configuration.
Minimal Git metadata contains the exact `git rev-parse HEAD` value from the
checkout so the synthetic evaluation can report the real repository revision.
No credentials are passed to pytest.

## Explicit source snapshot policy

The runner does not copy the worktree recursively. `PYTHON_SOURCE_ROOTS`
permits only regular `*.py` files under `app/` and `tests/`; cache and hidden
directories are skipped, unapproved regular files are left out, and symlinks
or special files in those source trees fail the snapshot. `APPROVED_SCRIPT_FILES`
and `APPROVED_SOURCE_FILES` enumerate the runner/test scripts and exact
configuration, static, benchmark, Kubernetes, target-app, and runbook inputs
needed by the suite. Files outside these scopes are never traversed or copied.
All manifest paths are repository-relative, checked for traversal and
containment, and must resolve to regular files. Required test data and the
dependency lock are SHA-256 pinned in `APPROVED_INPUT_SHA256`.

Every new Python file placed anywhere under `app/` or `tests/` is automatically
included in the snapshot. Review every newly added or changed Python file in
those trees before merging to ensure it contains no secrets, unsafe local
configuration, or unintended sensitive content. Snapshot inclusion is not a
security approval. Adding a directory or input category outside the existing
policy requires an explicit manifest or policy change and corresponding tests;
do not expand the policy implicitly.

The source `.env.example` is not copied. Its required Grafana password setting
is checked to occur once and be blank, then the snapshot receives only a
generated one-line fixture for that assertion; `.env` and all other `.env.*`
files are not selected. `config/operators.json`, virtual environments,
backups, generated reports, caches, and unrelated scripts/configuration are
not part of the manifest. Git history and objects are not copied into the
snapshot; selected file bytes are materialized from Git blobs named by the
expected revision's tree. Each manifest entry records its repository-relative
path, Git blob object ID, and SHA-256 digests for the source and resulting
snapshot bytes. The `.env.example` projection records distinct source and
snapshot digests. A minimal generated `.git/HEAD` is supplied for the existing
revision lookup.
The generated `incidents.db` symlink to `/tmp/kybernaut-ci.sqlite3` is the
only symlink exception; it preserves the existing isolated temporary database
behavior and is created by the runner rather than copied from the worktree.

Before pytest starts, `scripts/ci_network_isolation.py` runs two controlled
canaries against local-only listeners. The host listener binds only to the
Docker bridge gateway; the peer listener has no published host port:

* A networked control container must reach a listener on the runner host, and
  the listener must record exactly that control connection.
* A networked control container must reach a peer container, and the peer
  listener must record exactly that control connection.
* Containers launched with `--network none` must fail both connection probes;
  neither listener may record a connection from an isolated probe.

The harness also starts a synthetic listener on the Docker bridge gateway for
the pytest-container child-process probe. Before pytest starts, a networked
control container must connect and send a run-specific payload. The actual
pytest container entrypoint then starts a Python child process that attempts a
connection to the same listener with a 1.5-second socket timeout; the parent
allows five seconds for the child process. A successful connection is a
failure; a connection error is recorded as denied, while a child timeout or
any other unexpected result fails the harness. Listener records are compared
before and after the child attempt; only the expected control record may be
present. This is the configured assertion, not evidence that isolation
succeeded in a real run. No successful Docker or GitHub Actions execution has
been observed here.

The runner requires an explicit expected source revision and performs a
read-only preflight before creating the report directory or starting Docker.
It compares that revision with `HEAD`, rejects tracked and untracked worktree
changes, and rejects ignored Python files under `app/` or `tests/` and ignored
files explicitly selected by the snapshot manifest. It repeats the preflight
after snapshot creation. The snapshot is therefore built from a clean checkout
matching the expected revision; no dirty paths or file contents are recorded.
Preflight does not clean or modify the checkout.

The runner inspects the actual pytest container configuration before starting
it and writes schema-version-6 evidence only after pytest has produced its
JUnit report and cleanup has run. The bundle contains:

* a UUID test-run identifier, source revision, UTC start/completion times,
  exact Python runtime, approved base-image reference, platform, and lockfile
  checksum;
* preflight schema version, expected and actual revision, clean worktree and
  post-snapshot status;
* source-snapshot schema version, expected revision, deterministic manifest
  of Git blob identities and source/snapshot content digests, and a SHA-256
  digest over the canonical manifest;
* the actual inspected pytest container ID and image ID, network settings,
  read-only root/source mount state, designated report mount and `/tmp`
  tmpfs, non-root user, privilege/capability settings, and namespace modes.
  Environment variables and host mount source paths are deliberately omitted;
* each canary's tested network mode, process exit code and expected output
  marker, plus the listener's timestamped source address and exact run-bound
  control payload. Both listener records must show exactly one control and no
  additional connection;
* the child-process probe's schema version, run ID, scope, synthetic target,
  network mode, child-process indicator, exit code, marker, and outcome; the
  enclosing evidence adds the inspected pytest container ID and bridge
  listener records before and after the attempt. The control record must be
  unchanged, and the validator binds the probe run and container IDs to the
  enclosing run and inspected pytest container;
* the JUnit file's SHA-256, byte length, and filesystem timestamp;
* cleanup schema version 3 and aggregate status; run-bound records for both
  the `host-canary-listener` and `pytest-child-listener`, including socket and
  thread shutdown outcomes; and for each peer and pytest container and test image, the
  run identifier, human-readable resource name, authoritative full Docker
  resource ID, cleanup command exit code, bounded outcome summary, and
  post-cleanup verification result. The validator binds each cleanup ID to
  the peer ID in the canary, pytest container ID in the run, or test image ID
  recorded from Docker inspection. Listener cleanup IDs must be exactly the two
  expected listener roles, unique, and bound to the run UUID; missing, duplicate,
  failed, contradictory, or cross-run listener records are rejected. Diagnostic
  fields contain fixed summaries, not command output.

`scripts/validate_network_isolation_evidence.py` takes both the evidence JSON
and the JUnit XML. It rejects unsupported schemas, malformed or contradictory
records, mismatched run IDs/revisions/container identity, unapproved runtime
or lock pins, unsafe inspected configuration, incomplete or contradictory
cleanup/preflight records, and any JUnit checksum/size/timestamp mismatch.
Schema-v2 bundles do not contain cleanup or preflight evidence and are
intentionally rejected by the schema-v6 validator; they cannot be upgraded
into qualification evidence after the fact. Pytest embeds the run UUID in
every JUnit testcase classname, so a report from another run is rejected even
if its checksum is updated in a copied bundle. The validator does not treat a success boolean or
connection counter as proof. Schema-v1 retained bundles are intentionally
rejected; the missing raw records and run/report association cannot be
reconstructed after the fact.

The evidence is sanitized and generated by the runner, not cryptographically
signed. The manifest is structurally validated but is not independently
authenticated. Its checksums bind the submitted report to the bundle but do not
authenticate artifacts against an actor able to replace both files. JUnit
output must separately pass `scripts/validate_junit_report.py`; artifact
upload is gated on both validators and retained for seven days. Validation
and upload happen outside the pytest network boundary.

## Clean-checkout preflight (implemented; not run for qualification)

Before any separately authorized qualification, record the approved immutable
source revision and verify that checkout `HEAD` matches it. Require a clean
worktree (`git status --porcelain` is empty) so the tested source cannot
silently differ from the approved revision. Use Python 3.12 for the initial
qualification target, as recorded in
[`CI_OWNERSHIP_DECISION.md`](./CI_OWNERSHIP_DECISION.md), and verify the
actual test-image interpreter reports Python 3.12.

Verify the checked-out `requirements.txt` SHA-256 against the reviewed value
pinned in both the harness and evidence validator before building the test
image. Record the revision, clean-worktree result, runtime, and lock checksum
for review. A revision mismatch, dirty checkout, unsupported runtime, or lock
checksum discrepancy must stop preflight rather than be repaired implicitly.
The harness requires `--expected-revision`; both workflows pass their
`GITHUB_SHA`. It performs these checks before Docker and repeats the clean
checkout/revision check after source snapshot creation. Selected snapshot bytes
are read from Git blobs in that expected revision, not copied from mutable
worktree file contents. The snapshot manifest records those Git blob object
IDs and SHA-256 content digests; requirements bytes are read once, checked
against the approved digest, compared with the expected-revision blob, and
those same bytes are written to the Docker build context. These checks are
implemented and unit-testable, but have not been executed as part of an
authorized qualification run. The evidence manifest is not independently
authenticated or signed.

## Cleanup acceptance criteria and current implementation

A qualification result is acceptable only when cleanup is explicitly
verified and reviewable, separately from test and network-probe success:

* **Listener:** close the listener socket, join its serving thread within a
  bounded timeout, and verify the thread stopped. A timeout or inability to
  verify shutdown is incomplete cleanup.
* **Containers:** after normal completion or timeout, verify that the named
  pytest and peer-listener containers are stopped and removed. If the test
  timed out, record the timeout and forced-removal outcome; a successful test
  result cannot be inferred from a timed-out run. Short-lived control probes
  use Docker `--rm`; their removal is configured but is not independently
  inspected by ID.
* **Image:** verify that the run-specific image is removed. Do not remove
  unrelated containers or images; cleanup must target only IDs/tags created
  by that invocation.
* **Commands and timeouts:** retain cleanup command exit codes and bounded
  timeout outcomes. A nonzero exit, timeout, missing resource identity, or
  failed post-cleanup inspection is cleanup failure, not success.
* **Ambiguous or incomplete outcomes:** missing cleanup records, inability to
  inspect a resource, or disagreement between command results and inspection
  must leave the run unqualified. Preserve available sanitized evidence and
  escalate through the approved failure path before retrying or changing the
  environment.

The harness checks listener-thread termination after a bounded join and
records bounded cleanup-command and post-removal inspection outcomes for the
named peer and pytest containers and unique image. Every probe container is
given a run-specific name and two Docker labels before execution; the image
build and long-lived containers receive the same run/resource label pair.
After a Docker client timeout, the harness queries the daemon using both exact
labels, validates returned full resource IDs, removes only a single matching
ID, and queries again to verify removal. For resources whose IDs were returned
by create/inspect before cleanup, a discovered ID must exactly match that
authoritative ID. Names and labels select candidates only; they are not proof
of identity. Cleanup records carry both the run UUID and authoritative
resource ID, and schema-v6 validation rejects missing, duplicate, malformed,
mismatched, or cross-run identities. Schema-v5 evidence and cleanup schema-v2
bundles are intentionally rejected; they lack listener identity bindings and
cannot be upgraded into qualified evidence.

A missing resource immediately after a timed-out operation remains
**unverified**, because late daemon-side creation cannot be excluded by that
bounded observation. Verified removal is distinct from failed and unverified
cleanup. A resource ID that was expected but is already absent before a
normal cleanup command is also unverified; absence alone does not prove this
invocation removed it. After a removal command, verification requires a
successful bounded query with no matching run-associated resources. A
different or additional matching ID, malformed/duplicate IDs, discovery
errors, cleanup timeouts/nonzero exits, or post-cleanup query errors cannot
produce cleanup success. The harness does not attempt to remove a newly
appearing unexpected candidate.
Timeout recovery status is retained in cleanup records and written to stderr;
cleanup failures are attached to the primary exception and also logged. A
nonzero cleanup result causes a nonzero harness outcome. If the primary run
fails before a complete evidence bundle can be produced, the runner explicitly
logs that no complete evidence bundle was emitted and summarizes available
cleanup identities/outcomes without replacing the original failure. This is
implementation evidence only; cleanup behavior has not been observed in a
Docker run.

Evidence serialization is completed before publication. The runner writes a
temporary file in the report directory, flushes it, and publishes it without
overwriting an existing destination. If assembly or publication fails, the
runner reports that no complete bundle was emitted and summarizes all recorded
listener and Docker cleanup outcomes. The bundle is not considered complete
merely because a partial output file exists.

The evidence validator requires complete JUnit suite summary counts and
checks those counts against testcase annotations. It also requires the
pytest exit status and JUnit failure/error counts to agree in both directions;
skipped-only reports are consistent only with a zero exit code. This structural
consistency check does not independently establish that the tests exercised
the intended source or that the report is authentic.

Probe containers still use Docker `--rm` for normal completion, but a timed-out
client does not rely on that flag: it performs the label-scoped recovery and
verification above. The evidence is structurally validated but is not
cryptographically authenticated.

## Unresolved owner decisions

No names or approvals are inferred here. Before an authorized qualification,
the ownership record must identify:

* controlled-runner owner and security-maintenance owner;
* authority to authorize this specific qualification run;
* authority to approve network exceptions and permitted destinations;
* evidence reviewer and final qualification approver; and
* escalation and containment authority for isolation failure, timeout,
  cleanup failure, or ambiguous evidence.

These assignments and the applicable failure and exception procedures remain
unresolved in [`CI_OWNERSHIP_DECISION.md`](./CI_OWNERSHIP_DECISION.md).

## Base-image pin maintenance

The workflows use the official Python Docker image repository, pinned to
`linux/amd64` manifest digests:

| Workflow | Runtime | Image reference |
| --- | --- | --- |
| `ci.yml` | Python 3.12 | `python@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1` |
| `tests.yml` | Python 3.11 | `python@sha256:b7a9e5f6a93d6d4a31f129bbd4bdee69efccb6ded0fb230d78aafaf37af98377` |

The helper checks that each reference matches its workflow's Python minor
version and that the resulting image is `linux/amd64`. To update a pin, verify
the replacement digest against the official Python image manifest and target
runtime, update the workflow, runner, and evidence-validator allow-lists
together, and run the full isolated suite plus both canaries for both Python
versions. The approved `requirements.txt` checksum is also enforced by the
runner and evidence validator; an intentional lock update must update both
checksums only after the lock has been reviewed and qualified.

## Git package pin maintenance

The Python base images are Debian 13 (trixie). The image builder uses the
immutable Debian snapshot `20261026T000000Z` and installs the exact
`git=1:2.47.3-0+deb13u1` package. The build verifies both the Debian package
version and `git --version`; any missing package, version mismatch, apt failure,
or Docker build failure stops the runner. Apt uses only that snapshot's trixie
main index for this installation, with signature verification retained and
expired snapshot metadata accepted explicitly.

The snapshot fixes repository metadata and package payloads for this build, but
the build still depends on the snapshot service being available. When refreshing
Git, verify the candidate from the intended snapshot against both pinned base
images, then update the snapshot timestamp and package version together and
rerun both isolated suites. The official base-image digests and approved
`requirements.txt` checksum are independent pins and must not be changed as
part of a Git refresh.

This boundary applies to pytest and its child processes, not checkout, image
build/dependency installation, YAML or report validation, or artifact upload.
Those setup and reporting phases remain network-capable. Docker containers
share the runner kernel, so this is not a defense against a kernel or container
runtime escape. The source and harness document intended controls; neither
implementation nor static evidence proves that isolation worked in a run.
Hosted-runner behavior, artifact results, and branch-protection status remain
unverified until the approved workflow and repository settings are reviewed.
