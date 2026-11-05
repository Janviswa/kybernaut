# Observability Resilience Test Plan

**Status: plan only.** No test in this document has been executed or
authorized. Each runtime test requires a separate review and explicit
authorization for its exact scope. Tests must not target the existing Kybernaut
application, Loki, Alloy, Prometheus, Grafana, or Portainer containers.

## Current configuration reference

The current application path is:

```text
application → Docker syslog driver (TCP, rfc5424micro)
  → Alloy TCP syslog receiver at host 127.0.0.1:1515
  → Loki at http://loki:3100 on kybernaut-observability_backend
```

The fixed tag mapping is:

| Docker tag / RFC5424 APP-NAME | Loki label |
| --- | --- |
| `kybernaut-order` | `service="order"` |
| `kybernaut-payment` | `service="payment"` |
| `kybernaut-user` | `service="user"` |
| `kybernaut-inventory` | `service="inventory"` |

Configured logging options are `syslog-format=rfc5424micro`,
`mode=non-blocking`, and `max-buffer-size=4m`. The current Compose stack has
only Loki and Alloy on a dedicated bridge network; Loki has no host-published
port, Alloy is published only on host loopback port 1515, and Loki data uses
`kybernaut-observability_loki-data`. The Alloy WAL is disabled. Loki's
configured retention is `168h`.

These settings and the reported Phase E.6 evidence are documented in
[`OBSERVABILITY.md`](./OBSERVABILITY.md). The existing running stack and its
data are not test fixtures.

## Phase E.8C — Local prerequisite investigation

Read-only local inspection on 2026-10-22 found the following. These findings
do not authorize a test or runtime change.

### Docker syslog acceptance and drop evidence

- **Status: Partially resolved.** Docker client and daemon are both
  `29.1.3` (API `1.52`). `docker info` reports the daemon default logging
  driver as `json-file`; read-only inspection of the four application
  containers confirms each is configured with the `syslog` driver and the
  expected TCP address, fixed tag, `rfc5424micro`, non-blocking mode, and
  `4m` buffer.
- **Evidence reviewed:** Docker `run --help`, the locally installed
  `docker-run(1)` and `docker-logs(1)` manuals, `docker info`, and the
  containers' logging configuration metadata. The installed manual states
  `docker logs` works only with `json-file` and `journald`; it does not offer
  log retrieval for the configured `syslog` driver. The inspected CLI and
  metadata expose logging options, but no per-event remote-acceptance,
  buffer-occupancy, or dropped-message counter was identified. This is a
  finding about the inspected local interfaces, not proof that no internal
  or separately supported daemon diagnostic exists.
- **Interpretation:** A successful application/generator write is local
  evidence only. Container state and configured log options do not prove
  Docker delivered an event to Alloy. A TCP write/acknowledgment is not
  evidence that Alloy parsed, forwarded, or Loki ingested that event. An
  individual count-only Loki query for a unique event ID can establish that a
  matching entry became queryable in Loki, but cannot locate the delivery
  stage for missing events.
- **Needed to resolve:** Version-matched Docker syslog-driver documentation
  or source identifying supported delivery/drop/queue signals, plus an
  independently validated receiver-side per-event acknowledgment or
  counter if receiver acceptance must be measured. Record unobservable
  stages as such; do not infer Docker drops from missing Loki results.
- **Tests affected:** OBS-R-04 remains blocked as a buffer-saturation test
  unless a supported, pinned-version buffer occupancy/saturation signal is
  identified. OBS-R-03 and outage portions of OBS-R-04 may measure event
  outcomes and recovery, but attribution of gaps to Docker, transport, Alloy,
  or Loki remains unverified without independent stage evidence. OBS-R-01
  and OBS-R-02 can establish only per-ID Loki queryability, not remote
  acceptance at intermediate stages.

### Storage quota enforcement

- **Status: Unresolved for a project-wide bound on this host.** `docker info`
  reports storage driver `overlayfs`, Docker root `/var/lib/docker`, and
  driver status `io.containerd.snapshotter.v1`. The Docker root is on the
  host's `/` filesystem, `/dev/nvme0n1p4`, mounted as `ext4` with active
  options `rw,relatime`; no project-quota mount option was reported. The
  installed `docker-run(1)` manual's legacy `overlay2` size guidance requires
  an XFS backing filesystem mounted with `pquota`; that does not establish a
  quota mechanism for this containerd overlayfs snapshotter. `docker system
  df` reports usage totals, not an enforceable quota. An attempt to read ext4
  feature metadata was denied by host permissions, so ext4 project-quota
  capability is not established.
- **Interpretation:** CPU, memory, and PID limits are per-container
  resource limits, not aggregate storage quotas. No inspected evidence proves
  that a 2 GiB project-wide ceiling would include all container writable
  layers and named volumes. Availability of quota-related utilities or
  Docker's generic `--storage-opt` option is not proof that a usable quota
  is active for this backend.
- **Needed to resolve:** A host administrator must establish, without
  affecting existing Docker data, a supported and enforceable quota boundary
  covering every disposable container layer and volume, and verify the
  effective limit before the test. A separately isolated disposable VM or
  Docker host with a quota-enforced filesystem is an alternative. Do not
  change current filesystem or Docker storage settings for this plan.
- **Tests affected:** OBS-R-02 and OBS-R-04 remain blocked on this host until
  the aggregate storage bound is enforceable and verified. OBS-R-03, as
  written, inherits the OBS-R-02 resource-limit preconditions and is also
  blocked until its approved disposable environment satisfies them. Do not
  interpret free space or `docker system df` as quota enforcement.

### Loki retention deletion evidence

- **Status: Unresolved for physical deletion.** The local image metadata
  identifies the available `grafana/loki:3.0.0` image by digest
  `sha256:757b5fadf816a1396f1fea598152947421fa49cb8b2db1ddd2a6e30fae003253`.
  The current configuration enables retention, sets `retention_period: 168h`,
  `compaction_interval: 10m`, `retention_delete_delay: 2h`, and uses the
  filesystem delete-request store. Local documentation search found no
  installed Loki manual or version-specific deletion diagnostics; image
  labels identify the source repository but do not document a supported
  completion signal.
- **Interpretation:** These settings describe configured retention and
  deletion scheduling, not proof that a particular record was physically
  deleted. A query miss establishes only that the record was not returned by
  that query. No metric, endpoint, marker-file procedure, or physical-storage
  diagnostic is asserted here because none was verified from the local
  version-matched evidence.
- **Needed to resolve:** Version-matched Loki 3.0.0 documentation or
  supported diagnostics that establish compactor deletion completion for
  synthetic records, with a way to correlate completion to the test data.
  If that evidence cannot be established, report query expiration only and
  leave physical deletion unverified.
- **Tests affected:** OBS-R-05 remains blocked for any claim or pass criterion
  of physical deletion until a supported diagnostic and version-specific
  timing are established. A future, separately authorized query-visibility
  test must be reported as expiration from query results only, not proof of
  physical removal. Current seven-day retention settings and data must remain
  untouched.

## Global safety and test controls

1. No test may access `/opt/kybernaut`, `kybernaut-api.service`, host port
   8000, Kubernetes, live remediation, or production data.
2. Do not use current application or observability containers as fault
   injection targets. Never stop, restart, recreate, rename, remove, or change
   their logging options under this plan.
3. Do not run `docker compose down`, `down -v`, prune, broad cleanup, or any
   command that can delete current data. Never reuse or delete the existing
   Loki volume.
4. Do not pull images, build images, deploy, or change runtime settings as
   part of this plan. A later test proposal must identify exact already-local
   pinned images and receive approval before creating resources.
5. Prefer a separately reviewed disposable Compose project with a unique
   project name, dedicated network, unique container names, and brand-new
   dedicated volumes. Use synthetic data only. Verify project/resource names
   do not overlap existing resources before creation. The approval must
   provide exact names and storage locations; do not start with unresolved
   placeholders. Before creation, verify the Docker context, existing
   resource inventory, network isolation, and volume paths read-only. Stop
   on any collision or uncertainty.
6. Pin and record image IDs, versions, configuration hashes, resource-limit
   settings, and test parameters before each authorized run. Validate test
   configs with the pinned binaries before starting services. Verify
   configured CPU, memory, and PID limits from the created containers'
   effective runtime settings, and report those separately from measured
   high-water marks.
7. Put one unique marker, such as `canary_id=<unique-id>`, exactly once in
   each single-line synthetic event. Query each ID individually with a
   count-only expression that filters on that marker and sums matching log
   entries across stream series. Require one match under the expected service
   label and zero under the other allowed labels. A per-ID count of one then
   establishes one matching Loki entry in that query window, not receipt by
   an upstream stage; a total across IDs or a count of result series does not
   prove per-ID uniqueness. Do not return ordinary application log messages
   or payloads.
8. `mode=non-blocking` and `max-buffer-size=4m` establish a bounded buffer,
   not an end-to-end delivery guarantee. A successful generator write proves,
   at most, local acceptance by that write path; it does not prove Docker
   driver delivery, TCP peer receipt, Alloy parsing/forwarding, or Loki
   ingestion. A TCP acknowledgment is not an application-level acknowledgment
   that Alloy accepted or forwarded a syslog event. Do not assume an
   authoritative per-event Docker drop counter or receiver-accepted counter
   exists. Before a test, verify which supported, version-specific counters
   or diagnostics are available. If no independent evidence measures a stage,
   report that stage as unobserved.
9. Before attributing a missing event, retain distinct counts for events
   attempted, locally accepted by the generator's write call, any
   independently measured receiver acceptance, uniquely queryable Loki
   entries, duplicates, and unaccounted IDs. Establish that the query covers
   each event timestamp, measured clock skew, and the retention window; check
   all expected and allowed service labels; and record collector readiness.
   If these controls do not identify the loss point, record the event as
   unaccounted rather than guessing.
10. Any test involving a real runtime fault, restart, saturation, or deletion
    requires a separate explicit approval after review. Approval must specify
    project, services, event/rate/duration limits, resource ceilings, stop
    thresholds, and cleanup owner. This plan itself grants no such approval.

## Evidence levels

- **Configuration validation:** a pinned binary accepts a configuration.
  This does not establish delivery, readiness, or resilience.
- **Synthetic acceptance:** known synthetic events are counted individually
  under their expected fixed labels in a time window expanded by measured
  clock skew. This establishes only that those entries were queryable in the
  tested path and conditions; it does not establish receiver acceptance for
  events absent from Loki.
- **Resilience evidence:** controlled, isolated fault or load tests produce
  measurable send/receive, readiness, recovery, and resource evidence. A
  bounded test cannot establish guaranteed lossless behavior under every
  workload or outage. Do not report a stage as measured unless its independent
  evidence source and limitations are documented.

## Test plans

### OBS-R-01 — Baseline and synthetic ingestion

- **Risk:** Low.
- **Type:** Synthetic; no fault.
- **Preconditions and approvals:** Separate approval to create and run a
  disposable test project. Confirm unused project, container, network, and
  volume names; pinned images are already local; both disposable services pass
  config validation and readiness checks.
- **Scope:** Only the disposable Docker syslog sender, Alloy, and Loki. Do not
  address the current stack. Use the same TCP/RFC5424 behavior and fixed
  allowlist mappings intended for production.
- **Procedure:** Send one unique, valid synthetic event per expected fixed
  tag, with valid RFC5424 structured-data syntax and the tested Docker
  `rfc5424micro` syslog-driver options. Record each attempted ID, generator
  write result, and event timestamp in UTC. Verify sender and Loki clock
  offsets against a common time source before sending. After the final send,
  allow up to 60 seconds for ingestion, then query every ID individually with
  a count-only expression. The query interval must cover the event timestamp
  plus the measured maximum clock offset; also check each ID across all four
  allowed service labels to detect mislabeling. Do not query or print message
  content.
- **Pass/fail evidence:** Pass synthetic acceptance only if all four IDs
  are individually queryable exactly once under the expected label, have no
  match under another allowed label, and arrive within 60 seconds of their
  send time. Separately report attempted IDs and successful local write
  calls; neither is proof of remote receipt. If clock offset cannot be bounded
  or the exact-ID query cannot establish per-ID counts, mark the result
  inconclusive. Any missing, duplicate, mislabeled, or late event is a
  failure requiring diagnosis; do not resend automatically.
- **Cleanup/recovery:** Stop only disposable senders, preserve test evidence,
  then remove only resources explicitly named in the test approval. Do not
  remove any existing resource or volume.
- **Stop conditions:** Unexpected resource-name collision, readiness failure,
  nonzero sender exit, unexpected label, more than one match, or any
  application/runtime impact.

### OBS-R-02 — Bounded sustained-load behavior

- **Risk:** Medium.
- **Type:** Synthetic bounded load; no fault.
- **Preconditions and approvals:** Separate explicit load-test approval.
  OBS-R-01 passes. Disposable-only project and fresh volumes. Enforce
  per-container limits before starting: each sender and Alloy at no more than
  0.5 CPU and 256 MiB memory; Loki at no more than 1 CPU and 1 GiB memory;
  each container has a PID limit of 128. The disposable project must have an
  externally enforced 2 GiB storage ceiling that covers test volumes and
  container writable storage; if the host cannot enforce and verify that
  bound, do not run the test. Docker Compose resource settings alone must not
  be assumed to impose a project-wide storage quota. No host application port
  is published.
- **Scope:** Synthetic sender(s), disposable Alloy and Loki only.
- **Procedure:** Send exactly 1,000 uniquely numbered, fixed-size synthetic
  events total at no more than 10 events/second, for no more than 100 seconds.
  Use one fixed approved tag per run; do not create per-event labels. The
  generator records attempted IDs, successful local write calls, and failed
  writes in a content-free counter report. After the 60-second ingestion
  timeout, query each ID individually with count-only expressions over an
  interval covering its timestamp and the measured clock offset. Record
  receiver acceptance only if a validated, independent per-event signal is
  available; otherwise mark it unobserved. Record effective configured
  container limits separately from CPU, memory, PID, and storage high-water
  measurements.
- **Pass/fail evidence:** Pass if the generator accounts for all 1,000
  unique attempts, no local write errors occur, every ID appears exactly once
  under the fixed expected label with no match under another allowed label
  within 60 seconds, and measured resources remain below their enforced
  ceilings. Aggregate totals alone are insufficient. Any discrepancy is a
  failure; distinguish generator/write results, independently measured
  receiver acceptance if available, Loki counts, and unaccounted IDs. No
  result justifies a lossless claim outside these parameters.
- **Cleanup/recovery:** Stop only disposable senders and services; retain
  count/resource evidence. Remove only approved disposable resources after
  confirming their exact names and paths.
- **Stop conditions:** Any ceiling reaches 80% (warning) or 90% (stop),
  storage reaches 80%, observed host pressure, readiness loss, unexpected
  service access, more than 100 seconds of generation, or any non-synthetic
  data. The ingestion/query settle period is additional and must be included
  in the approved total test window.

### OBS-R-03 — Collector outage and recovery

- **Risk:** High.
- **Type:** Real runtime fault, disposable environment only.
- **Preconditions and approvals:** Separate explicit authorization for the
  exact outage target and duration. OBS-R-01 passes. Disposable project,
  isolated network, fresh volumes, resource limits from OBS-R-02, and an
  agreed independent controller able to record event attempts and readiness.
  Never stop or impair the current Alloy or Loki.
- **Scope:** One disposable collector per test case: Alloy unavailable while
  disposable Loki remains ready, then (in a separately approved case) Loki
  unavailable while disposable Alloy remains running. Do not make both
  unavailable simultaneously.
- **Procedure:** Send 20 unique synthetic pre-outage events at no more than
  5/second. Cause one approved collector outage for at most 30 seconds. While
  it is unavailable, send up to 100 unique events at no more than 5/second;
  then restore that disposable collector using the approved service-specific
  action. Once both services are ready, send 20 new post-recovery sentinels.
  Record timestamps and readiness transitions; do not retry events
  automatically. Query each ID individually with count-only checks and a
  60-second ingestion timeout. Cover each event timestamp plus measured
  clock offset, and record sender writes, independently measurable receiver
  acceptance (if available), and Loki results separately. Keep separate
  counts for before, during, and after the outage.
- **Pass/fail evidence:** Record the exact attempted count, sender-reported
  failures, successful local write calls, independently measured receiver
  acceptance if available, per-ID Loki matches, duplicates, unaccounted IDs,
  and time to readiness/recovery. Local writes do not prove remote delivery.
  Pass the recovery criterion only if both services
  regain readiness within 60 seconds after restoration and new post-recovery
  sentinels are each found exactly once within 60 seconds. During-outage
  delivery is reported as measured, not presumed. Any difference between
  attempts and unique matches is an observed gap; attribute it to Docker's
  buffer, transport, parser, or query only when independent, version-validated
  evidence isolates that stage. Otherwise classify it as unaccounted.
- **Cleanup/recovery:** Restore readiness immediately, stop only the
  disposable sender, and preserve diagnostics/counters. If readiness does
  not recover within 60 seconds, stop the test and use only the preapproved
  disposable recovery procedure.
- **Stop conditions:** Any connection to a current service, unexpected
  impact, resource warning/limit, outage longer than 30 seconds, failure to
  restore readiness within 60 seconds, or any loss of test scope.

### OBS-R-04 — Non-blocking buffer saturation

- **Risk:** **High — separate explicit approval required.**
- **Type:** Synthetic load plus a real collector fault, disposable environment
  only.
- **Preconditions and approvals:** Written approval must identify exact
  disposable services, event volume/rate, buffer and resource caps, and
  recovery owner. OBS-R-01 and OBS-R-02 pass. Use a fresh isolated project;
  never change the live `4m` buffer or test against the current stack. Enforce
  at most 0.5 CPU and 256 MiB per sender/Alloy, 1 CPU and 1 GiB for Loki,
  PID limit 128, and an externally enforced 2 GiB storage ceiling covering
  test volumes and writable storage. Before authorizing a saturation test,
  identify a supported, pinned-version signal that can show buffer occupancy
  or saturation. If none is available, do not run OBS-R-04 as a saturation
  test; any separately authorized outage/load test does not satisfy this
  objective. Do not run if the storage ceiling cannot be enforced and
  verified.
- **Scope:** Disposable syslog sender(s), Alloy, Loki, and the test-only
  configured bounded logging buffer.
- **Procedure:** First establish a measured healthy-delivery control. Then
  make only the disposable Alloy endpoint unavailable under an approved
  duration and offer synthetic traffic at a capped rate, maximum 10
  events/second and 1,000 total. Observe sender write latency, errors,
  container memory, and received unique-ID counts. Do not increase rate or
  duration to force a desired outcome. Restore Alloy once any stop threshold
  is reached and query each test ID individually after recovery. This bounded
  procedure does not establish that the Docker buffer reached capacity.
  Unless a supported, independently validated signal proves buffer occupancy
  or saturation, describe results only as behavior during the tested outage
  and load; mark actual saturation unverified.
- **Pass/fail evidence:** Report attempted, successful local writes, failed,
  independently measured receiver-accepted events if available, per-ID Loki
  matches, duplicates, unaccounted IDs, configured limits, and measured
  resource high-water marks. A successful local write followed by no Loki
  match is an observed gap, not proof that Docker dropped the event. Attribute
  a drop only when validated evidence isolates the responsible stage; do not
  infer a precise buffer-drop count without a reliable driver/receiver
  counter. If saturation itself cannot be measured, the saturation objective
  is inconclusive. This test measures observed behavior, not losslessness.
- **Cleanup/recovery:** Restore the disposable endpoint immediately, verify
  both services ready, cease generation, preserve only aggregate evidence,
  then remove only approved disposable resources. No existing volume is
  eligible for cleanup.
- **Stop conditions:** Any sender/application blocking beyond 1 second,
  memory at 80% warning or 90% stop threshold, storage at 80%, host pressure,
  outage exceeding 30 seconds, readiness failing to recover within 60
  seconds, or accidental contact with the current stack.

### OBS-R-05 — Retention expiration and deletion

- **Risk:** **High — destructive to disposable test data; separate explicit
  approval required.**
- **Type:** Synthetic data plus real retention expiration/deletion in a
  disposable Loki instance.
- **Preconditions and approvals:** Separate approval for a disposable,
  short-retention configuration and its fresh volume. Never mount, edit, or
  reuse `kybernaut-observability_loki-data`; never change current `168h`
  settings. Validate the test-only Loki config with pinned Loki 3.0.0 before
  startup. Confirm pinned-version retention/deletion timing and the compactor
  delete delay from supported documentation/config before choosing a test
  window. If minimum retention behavior or deletion timing cannot be
  established, do not run.
- **Scope:** One disposable Loki, Alloy or synthetic push client, dedicated
  network and a new disposable data volume containing only unique synthetic
  records. No other data.
- **Procedure:** Configure an explicitly approved short retention period in
  the disposable configuration only, keeping compatible schema/compactor
  settings. Ingest known synthetic IDs and prove they are queryable before
  expiry. Wait beyond the configured retention period, delete delay, and at
  least two configured compaction intervals. Query the IDs count-only; inspect
  only supported compactor status/metrics and aggregate disposable-volume
  usage. Do not assume a query miss alone proves physical deletion.
- **Pass/fail evidence:** First require all seed IDs present before expiry.
  After the agreed deletion window, require expired IDs not queryable and
  supported compactor evidence that deletion completed. Do not assume a
  particular metric, status endpoint, or diagnostic exists: identify and
  validate the pinned-version evidence source before the test. If no supported
  evidence can establish physical deletion, report only query expiration as
  observed and physical deletion as unverified; a query miss or aggregate
  volume-size change alone does not prove the event's data was physically
  removed. Any still-queryable expired event or compactor error fails the
  query-expiration criterion.
- **Cleanup/recovery:** Preserve the disposable test report, then remove only
  the specifically authorized disposable project and its unique data volume
  after a second resource-name verification. Never run broad cleanup.
- **Stop conditions:** Any mount/reference to the existing Loki volume,
  unexpected non-synthetic stream, test config not accepted by Loki 3.0.0,
  retention timing uncertainty, storage ceiling warning, or evidence that a
  current configuration/data path is involved.

### OBS-R-06 — Disposable restart and recovery

- **Risk:** Medium.
- **Type:** Synthetic events plus real restart of disposable services only.
- **Preconditions and approvals:** Separate explicit authorization for each
  service restart. Disposable project, fresh volumes, passing baseline, and
  recorded pre-restart readiness. Current Loki and Alloy must not be targets.
  Record the exact disposable resource names and the named recovery owner in
  the approval before creation.
- **Scope:** Test Alloy restart and Loki restart as separate cases, never
  simultaneously. Keep the other disposable service ready.
- **Procedure:** In each case, send a fixed set of 20 unique synthetic events
  before the restart; execute one approved restart of the selected disposable
  service; record downtime and readiness polling at 1-second intervals for at
  most 60 seconds; then send 20 new unique post-recovery events. Query the
  two sets count-only after readiness and a 60-second settle period.
- **Pass/fail evidence:** Pass recovery if the restarted service becomes ready
  within 60 seconds, the non-target service remains ready, and all 20
  post-recovery event IDs each appear exactly once under the expected label
  within 60 seconds, with no matches under another allowed label. Query
  windows must include measured clock offset. Report pre-restart events
  separately and distinguish local writes from any independently measured
  receiver acceptance; do not attribute missing records without evidence.
- **Cleanup/recovery:** If the service is not ready after 60 seconds, use
  only the separately approved disposable recovery procedure. Stop the sender
  and preserve aggregate results; remove only named disposable resources
  after review.
- **Stop conditions:** Any restart of a current service, second restart
  attempt, failure to recover within 60 seconds, loss of the non-target
  service, resource ceiling warning, or unexpected data.

### OBS-R-07 — Isolation and label integrity

- **Risk:** Low for read-only inspection; medium for a synthetic unapproved
  tag check.
- **Type:** Simulated/static inspection, with an optional synthetic negative
  event in a disposable environment only.
- **Preconditions and approvals:** Read-only configuration/runtime inspection
  requires no runtime-change approval but must not display secrets or log
  content. The optional negative-tag event requires separate approval and a
  disposable project.
- **Scope:** Read-only inspection of the current Compose/config files and
  container metadata; optional synthetic invalid APP-NAME only in the
  disposable pipeline. Do not send an invalid tag to the current Alloy
  receiver.
- **Procedure:** Verify from Compose and `docker inspect` that only Alloy has
  host binding `127.0.0.1:1515`, Loki has no host port, both observability
  services use only `kybernaut-observability_backend`, Loki's data mount is
  `kybernaut-observability_loki-data`, and config mounts are read-only.
  Check there are no Docker socket or host application-log mounts. Inspect
  Alloy's static allowlist and enumerate recent Loki labels with count-only
  queries. In the disposable negative-tag case, send one unique synthetic
  message with an unapproved APP-NAME and query only its ID/count.
- **Pass/fail evidence:** Read-only checks pass if all bindings, network
  members, mount paths/modes, and four fixed mappings match the approved
  design, and the recent label inventory contains only the four expected
  service labels. Negative-tag case passes if no matching entry is found
  under any service label after both services are ready and the agreed
  60-second settle window. This verifies only the tested mapping and
  isolation state.
- **Cleanup/recovery:** No cleanup for read-only inspection. For the
  disposable negative-tag case, stop the sender and remove only explicitly
  named test resources after evidence capture.
- **Stop conditions:** Any unexpected host-published port, mount, network
  attachment, label, resource-name collision, or contact with the current
  Alloy/Loki stack; stop without changing the configuration.

## Authorization checklist for future execution

Before any test other than read-only inspection, the approval request must
state:

- Exact test ID, purpose, project/container/network/volume names, and pinned
  image IDs.
- Components affected and proof that no current Kybernaut resource is
  targeted.
- Event count, payload size, maximum rate, duration, query window, and
  success/failure thresholds.
- CPU, memory, PID, and storage ceilings, with enforcement method.
- Fault action and maximum duration, if applicable.
- Readiness, recovery, and stop thresholds; named operator responsible for
  recovery and cleanup.
- Exact disposable resources eligible for cleanup; no wildcard or broad
  cleanup commands.

Before creating resources, perform and record a read-only isolation check
against the approved Docker context: confirm every exact project, container,
network, and volume name is unused; verify dedicated network and volume
locations; and confirm the test sender has no route to the current
observability or application containers. The named recovery/cleanup owner
must be present and available for the entire test and recovery window.
Configured resource limits must be checked on the created resources, while
actual high-water measurements must be recorded separately. If the approved
storage ceiling cannot be enforced across test volumes and writable storage,
do not run OBS-R-02 or OBS-R-04.

Do not execute until the plan for that test has been reviewed and separately
authorized. Passing a baseline or canary does not authorize later load,
outage, restart, saturation, or retention-deletion testing.
