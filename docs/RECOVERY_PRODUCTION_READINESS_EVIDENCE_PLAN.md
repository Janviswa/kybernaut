# SQLite Recovery Production-Readiness Evidence Plan

**Status:** planning only. Synthetic qualification is **Complete with
Limitations**. Production recovery remains **NO-GO**. This plan does not
authorize production access, implementation, backup, restore, or operation.

Use this plan with the [Deployment Architecture Decision](./DEPLOYMENT_ARCHITECTURE_DECISION.md)
and [Disposable SQLite Recovery Toolkit](./DATABASE_RECOVERY_TOOLKIT.md).
Operators must supply evidence through an approved process; this plan does
not collect evidence from production systems.

## Evidence handling

For every evidence item, retain its source, collection date, scope, reviewer,
and approval reference in the operator-approved evidence repository. Use
sanitized configuration excerpts, deployment records, platform inventories,
and signed decisions where possible. Do not provide credentials, tokens,
secret values, environment-file contents, database files, database rows,
audit payloads, or unnecessary personal data. If a source contains sensitive
details, an authorized operator should produce a reviewed, redacted attestation
that still proves the acceptance criteria.

“Verified” below means established by authoritative evidence about the actual
deployment, not a repository default or synthetic test. Synthetic qualification
supports confidence in tested behavior but cannot establish deployment facts.
Evidence establishes facts; policy choices require explicit approval.

## Decision and evidence register

| Ref | Required evidence or decision; acceptable source and acceptance criteria | Owner | Sanitization / exclusions | Approval and gate | Consequence if open |
|---|---|---|---|---|---|
| D-01, D-02 | **Fact:** authoritative architecture/deployment record identifies the effective database path (provide an approved logical identifier, not secret-bearing raw config), host/container/volume topology, and selected backend. Accept when the database and API placement are unambiguous and match the supported design. **Policy:** select SQLite only if the approved topology and qualification support it; otherwise record the selected backend and commission its separate design. | Unassigned (platform/application architecture) | Required; omit secret paths, host identifiers not needed for review, and credentials. No database content. | Architecture owner approves topology/backend before Stage B decisions and Stage C design. | Source and backup scope cannot be safely defined; SQLite assumptions may not apply. |
| D-01 | **Fact:** deployment specification or platform-generated sanitized inventory states API instance count, process count per instance, worker/thread model, and whether any processes share the same database file. Accept when all writers and file-sharing boundaries are accounted for. | Unassigned (application/platform operations) | Required; no process environment dumps, tokens, or unrelated host data. | Architecture owner accepts evidence and decides whether the topology is compatible before Stage C. | Concurrency, locking, and recovery consistency remain unqualified. |
| D-01, D-02 | **Fact:** approved runtime/build inventory states SQLite library/runtime version, compile/runtime-relevant settings, effective journal mode and WAL/autocheckpoint settings if applicable, and storage type/filesystem characteristics including local/shared/network and persistence behavior. Use sanitized platform/build reports and an operator-approved, non-invasive configuration attestation; do not infer from synthetic fixtures. Accept when versions/settings and storage semantics are identified for the exact intended deployment. | Unassigned (database/platform operations) | Required; omit connection strings, secrets, raw environment files, and database contents. | Database/platform owner approves these facts before Stage C; design owner determines required additional qualification. | Online backup/WAL test results cannot be transferred to the deployment; filesystem locking/durability behavior is unknown. |
| D-03 | **Policy decision:** numeric RPO and RTO, service availability needs, and acceptable lock/contention behavior, with rationale and measurement boundaries. Accept only when service owner records target values, measurement definitions, and approver sign-off. Existing 24-hour/8-hour values are discussion assumptions, not approved targets. | Unassigned (service owner; operational approver) | Sanitized decision record; no sensitive operational details unless necessary. | Formal service-owner and operational approval required before Stage B and design sizing. | Frequency, retention, architecture, and restore acceptance cannot be selected. |
| D-04 | **Decision/evidence:** name accountable backup, restore, integrity-validation, and recovery roles; provide approved responsibility matrix, escalation path, coverage, and access boundary. Accept when primary and backup coverage and handoffs are assigned for each function. | Unassigned | Sanitized; do not include personal contact details beyond approved role directory references. | Service owner and security/operations approve before Stage B; role access must be approved before operation. | Failures and restore decisions have no accountable owner. |
| Recovery destination | **Policy decision:** specify destination class/location, separation from source failure domain, durability, access controls, encryption/key ownership, retention, deletion/cleanup, monitoring, and evidence of access review. Accept when each control has an owner, approved value, and validation method. | Unassigned (backup owner/security/platform) | Sanitized; never include credentials, keys, tokens, secret names/values, or exact sensitive locations in the general register. | Security and data/service owners approve before Stage C; control validation required before Stage E. | Exposure, unauthorized replacement, loss, or uncontrolled retention cannot be assessed. |
| D-05; recovery-specific | **Fact and policy:** determine whether the separate legacy JSONL remediation audit file remains authoritative/operationally required. If yes, provide a separately approved capture design defining event scope, capture boundary, gap/overlap detection, reconciliation to SQLite, access, integrity, retention, and recovery owner. Accept when a synthetic or isolated demonstration proves boundary handling and reconciliation criteria without production data. If no, obtain an accountable owner’s documented deprecation/records decision. | Unassigned (audit-history owner/compliance) | Required; no raw audit lines, identifiers, personal data, tokens, or secrets. Supply schemas, counts, redacted examples, or signed attestations only as approved. | Audit/compliance and service owners approve before Stage C; validation of chosen strategy before Stage D/E. | SQLite recovery may omit required audit history or create unexplained gaps/overlaps. |
| D-05; recovery-specific | **Policy decision:** specify SQLite audit history retention, integrity/tamper-evidence or independent export needs, review access, and reconciliation with JSONL and any external logs. Accept when authoritative record sources, retention periods, access roles, and review ownership are explicit. | Unassigned (audit-history owner/compliance) | Sanitized policy; no actual event payloads or personal data. | Compliance/security and service owner approve before Stage B; implementation controls verified before Stage E. | Audit completeness, retention, and evidentiary value remain unknown. |
| Recovery-specific | **Decision:** establish restore authority, required requester/approver separation, emergency exception process, and approval record. Accept when named roles, allowed scope, authorization evidence, and revocation/escalation rules are approved. | Unassigned (service owner/security) | Sanitized; no approval tokens, credentials, or personal data. | Security and operational owner approve before Stage B; authorization controls tested before Stage E. | A restore may be unauthorized or lack accountable approval. |
| Recovery-specific | **Decision:** approve validation and acceptance criteria: SQLite integrity, supported schema/lifecycle coverage, audit/link checks, provenance, content/consistency expectations, and independent reviewer. Specify treatment of incomplete/unsupported artifacts and criteria for declaring service data usable. Accept when criteria are measurable, versioned, and assigned to an independent reviewer. | Unassigned (database/recovery owner; application owner) | No production data is required for policy; later isolated evidence must be redacted. | Recovery owner and application/service owner approve before Stage C; isolated qualification before Stage D. | A technically readable database could be mistaken for a semantically acceptable restore. |
| Recovery-specific | **Decision:** define handling of `EXECUTING`, `EXECUTION_UNKNOWN`, workflow/handoff reconciliation-required, and other unresolved states after recovery. State explicitly that restore/validation does not authorize replay or remediation; define hold, investigation, evidence, and operator disposition. Accept when each unresolved class has a documented owner and fail-safe disposition path, tested in isolation. | Unassigned (incident/action owner; recovery owner) | Synthetic lifecycle fixtures are sufficient for qualification; exclude production claim/audit records. | Service and remediation-policy owners approve before Stage C; isolated handling test before Stage D/E. | Ambiguous actions may be incorrectly treated as complete, retried, or closed. |
| D-06 | **Policy decision (adjacent governance):** record self-approval rules and action classes requiring a distinct approver. Accept when exceptions, identity source, and enforcement evidence are explicit. | Unassigned (security/operations) | Sanitized policy; never provide approval tokens or credentials. | Security approval before any design that relies on approval-controlled recovery or live action; live-remediation enablement remains separately gated. | Recovery approval or related action governance may not provide separation of duties. |
| D-07 | **Policy decision (adjacent runtime posture):** record whether initial operation is dry-run-only and whether any live remediation is in scope. Accept only with an explicit decision and separately verified effective configuration; repository defaults are not evidence of deployment settings. | Unassigned (service owner/security) | Sanitized signed decision; no environment files, secret values, or runtime dumps. | Service/security approval before deployment design; live execution requires a separate approval and rollout gate. | Recovery design could be conflated with authorization for live actions. |

## Staged approvals

Passing a stage requires a recorded decision and evidence review. No stage
implicitly authorizes the next.

### Stage A — Evidence complete

- All applicable fact rows above have authoritative, current, sanitized
  evidence, source/date, accountable reviewer, and acceptance disposition.
- Missing facts are explicitly marked unresolved; synthetic test results and
  repository defaults are not substituted for production evidence.
- No production database contents or secrets are needed for this stage.

**Unlocks:** Stage B decision-making only.

### Stage B — Operator decisions recorded

- D-01/D-02 topology and backend are decided based on Stage A facts.
- D-03 numeric availability/RPO/RTO and contention objectives are formally
  approved; provisional 24h/8h assumptions are either accepted, changed, or
  rejected.
- Owners, destination/security/retention policy, audit strategy, restore
  authority, acceptance criteria, and unresolved-execution policy are recorded.
- D-05 through D-07 decisions are resolved where applicable; adjacent
  remediation decisions do not authorize live remediation.

**Unlocks:** request for a separately scoped production-specific design.

### Stage C — Production-specific design authorized

- Named design owner and reviewers accept the evidence and decision register.
- Design covers the selected topology, storage, backup/restore, provenance,
  audit capture/reconciliation, security, retention, failure handling,
  unresolved states, monitoring, ownership, and operational procedures.
- Threat model and any requirement for authenticated provenance are decided;
  SHA-256 alone is not creator authentication.
- Design approval explicitly excludes implementation and production use unless
  separately authorized.

**Unlocks:** a request for separately approved implementation.

### Stage D — Separately approved implementation and isolated qualification

- Written implementation scope, safety boundaries, and test environment are
  approved before code/configuration or runtime changes.
- Qualification uses isolated, production-representative infrastructure and
  synthetic or approved test data; it demonstrates backup/restore behavior,
  failure cases, security controls, audit reconciliation, acceptance criteria,
  and recovery of unresolved states.
- Independent reviewers accept results and operational runbooks, rollback,
  monitoring, access, retention, and cleanup evidence.

**Unlocks:** a request for explicit production-use approval only.

### Stage E — Explicit production-use approval

- All prior gates pass; accountable service, operations, security, and audit
  owners approve the exact implementation, version, topology, destination,
  schedule, access controls, retention, RPO/RTO, runbooks, and rollback plan.
- Effective production settings and access boundaries are independently
  verified through the approved change process.
- Approval is scoped, recorded, and revocable. No synthetic result or design
  approval is production-use authorization.

**Until Stage E approval is recorded, production recovery remains NO-GO.**

## Current handover

The highest-priority inputs are the sanitized authoritative topology/path and
process/storage facts (D-01/D-02), named owners (D-04), and formally approved
RPO/RTO (D-03). Next resolve destination/security/retention and audit-history
scope (D-05 and recovery-specific decisions), then restore authority and
unresolved-state policy. Ask accountable operators to submit those items
through an approved evidence channel. Do not collect them from production
systems as part of this plan.

Historical synthetic test results remain historical; this plan makes no fresh
test or production-readiness claim.
