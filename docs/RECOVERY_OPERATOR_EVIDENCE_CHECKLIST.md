# SQLite Recovery Operator Evidence Checklist

**Current gate:** Synthetic qualification **Complete with Limitations**;
Stage A **Pending — operator evidence required**; Stage B **Blocked**;
production recovery **NO-GO**.

Use this checklist with the [Production-Readiness Evidence Plan](./RECOVERY_PRODUCTION_READINESS_EVIDENCE_PLAN.md),
[Deployment Architecture Decision](./DEPLOYMENT_ARCHITECTURE_DECISION.md), and
[Disposable SQLite Recovery Toolkit](./DATABASE_RECOVERY_TOOLKIT.md). Submit
sanitized evidence through an operator-approved channel; no submission
channel is selected here.

Do not include credentials, tokens, secret values, environment-file contents,
database files or contents, audit payloads, or unnecessary personal data.
Where a source contains sensitive details, provide an authorized,
reviewed attestation or redacted excerpt sufficient to establish the stated
acceptance criteria. Record evidence source, date, scope, reviewer, and
approval reference. Repository defaults, previous planning notes, and
synthetic tests do not verify production facts.

For each row, replace `Pending` only after evidence is supplied and reviewed.
Keep evidence acceptance separate from policy approval.

## A. Deployment evidence

| Item / status | Required submission or decision | Acceptable evidence source | Responsible role | Sanitization | Acceptance criteria | Approval required |
|---|---|---|---|---|---|---|
| Effective database path and topology — **Pending** | Approved logical identifier for the effective database path; identify backend and where database, API, and storage are deployed. | Authoritative architecture record or reviewed deployment inventory. | Unassigned — platform/application architecture | Redact sensitive raw paths, host identifiers, secret-bearing configuration; no database data. | API/database placement, backend, storage boundary, and intended topology are unambiguous. | Architecture/service owner accepts topology and backend; does not alone authorize design. |
| Process/worker count and shared-file boundaries — **Pending** | Instance count, processes/workers per instance, concurrency model, and whether writers share a database file. | Approved deployment specification or sanitized platform-generated inventory. | Unassigned — application/platform operations | No environment dumps, credentials, or unrelated host data. | All writers and file-sharing boundaries are accounted for. | Architecture owner accepts compatibility before Stage B decisions. |
| SQLite version and journal/WAL configuration — **Pending** | Effective SQLite runtime/library version; journal mode and applicable WAL/autocheckpoint settings; relevant SQLite settings. | Approved build/runtime inventory and operator-reviewed non-invasive configuration attestation. | Unassigned — database/platform operations | No secret values, raw environment files, connection strings, or database content. | Facts are tied to the intended deployment/version and distinguish configured from default values. | Database/platform owner verifies; design owner accepts before design authorization. |
| Filesystem/storage characteristics — **Pending** | Storage and filesystem type; local/shared/network characteristics, persistence and locking behavior, and relevant durability assumptions. | Approved storage/platform specification or reviewed operator attestation. | Unassigned — platform/storage operations | Redact sensitive infrastructure identifiers; no credentials or data. | Storage semantics and failure domain for the selected database path are identified. | Platform/database owner accepts before design authorization. |

## B. Accountability

| Item / status | Required submission or decision | Acceptable evidence source | Responsible role | Sanitization | Acceptance criteria | Approval required |
|---|---|---|---|---|---|---|
| Platform/application owner — **Pending** | Assign accountable role(s) for deployment architecture and application behavior. | Approved responsibility matrix or service ownership record. | Unassigned | Role/team reference preferred; avoid unnecessary personal details. | Named accountable role and coverage/escalation path are recorded. | Service owner/operations approval. |
| Database/platform operations owner — **Pending** | Assign owner for SQLite/runtime/storage facts and operational support. | Approved responsibility matrix/on-call ownership record. | Unassigned | Use approved role references; no personal contact data beyond policy. | Primary and backup coverage and escalation path are identified. | Operations owner approval. |
| Backup and restore owner — **Pending** | Assign accountable backup, restore, integrity validation, and recovery roles; identify handoffs. | Approved responsibility matrix and recovery ownership record. | Unassigned | Use role references, not personal details. | Each activity has a named role, backup coverage, and escalation route. | Service owner and operations/security approval. |
| Audit-history owner — **Pending** | Assign owner for SQLite audit history and any separate JSONL audit history. | Approved audit/data ownership or compliance record. | Unassigned | No event contents or unnecessary personal data. | Scope, review responsibility, retention coordination, and escalation are assigned. | Audit/compliance and service owner approval. |
| Service owner and security approver — **Pending** | Identify accountable service decision owner and security approver for recovery design and controls. | Approved governance/approval matrix. | Unassigned | Role references only; no credentials or tokens. | Both responsibilities and approval boundaries are explicit. | Recorded service and security approval. |
| Recovery authorization and escalation authority — **Pending** | Assign who may request, approve, execute, halt, and escalate a restore; define emergency authority. | Approved recovery authorization policy and escalation procedure. | Unassigned | No approval tokens, secrets, or unnecessary personal data. | Authorities, separation/exception process, and escalation path are unambiguous. | Security and operational approval. |

## C. Policy decisions

| Item / status | Required submission or decision | Acceptable evidence source | Responsible role | Sanitization | Acceptance criteria | Approval required |
|---|---|---|---|---|---|---|
| Numeric RPO and RTO — **Pending** | Approve numeric RPO/RTO, measurement definitions, service availability needs, and rationale. | Signed service-level/recovery objective decision. | Unassigned — service owner and operational approver | Decision record; no sensitive runtime information required. | Numeric targets, measurement boundaries, and approvers are explicit. | Formal service-owner and operational approval. **RPO 24 hours/RTO 8 hours are provisional and not approved.** |
| Destination, access, encryption, retention, cleanup — **Pending** | Decide destination class/failure-domain separation, access controls, encryption/key ownership, retention, monitoring, cleanup, and access review. | Approved data-protection, storage, and retention policy. | Unassigned — backup owner, security, platform | Do not include exact sensitive locations, keys, credentials, secret names/values, or tokens. | Each control has an owner, approved policy/value, and a validation method. | Security and data/service owner approval. |
| JSONL audit-history scope and capture — **Pending** | Decide whether JSONL remains operationally authoritative; define event scope and capture boundary. If not needed, record an accountable deprecation/records decision. | Approved audit/records policy and system-of-record decision. | Unassigned — audit-history owner/compliance | No raw logs, event payloads, identifiers, or personal data. | Required sources and authoritative scope are explicit. | Audit/compliance and service owner approval. |
| JSONL reconciliation and retention — **Pending** | Define gap/overlap detection, reconciliation to SQLite, integrity/access controls, retention, and recovery ownership. | Approved audit recovery design; later qualification evidence may use synthetic/isolated data. | Unassigned — audit-history owner/compliance | Provide schemas or reviewed redacted attestations only; no raw event data. | Capture boundary, reconciliation outcome, retention, and owner are testable and approved. | Audit/compliance and service owner approval; isolated qualification later. |
| Restore approval and separation of duties — **Pending** | Decide requester/approver separation, allowed scope, emergency exception, revocation, and approval record. | Approved access-control and recovery authorization policy. | Unassigned — service owner/security | No approval tokens, credentials, or unnecessary personal data. | Authorized roles, identity source, separation rules, and exceptions are explicit. | Security and operational approval. |
| Restore validation and acceptance criteria — **Pending** | Define integrity, schema/lifecycle, provenance, audit/link, content consistency, independent review, and usable-restore acceptance criteria. | Versioned recovery acceptance policy approved by database and application owners. | Unassigned — recovery/database owner and application owner | No production data required to define policy; later evidence must be redacted. | Criteria are measurable, versioned, and have an assigned independent reviewer. | Recovery and application/service owner approval. |
| Unresolved execution/reconciliation states — **Pending** | Define disposition for `EXECUTING`, `EXECUTION_UNKNOWN`, workflow/handoff reconciliation-required, and other ambiguous states. State that restore/validation does not authorize replay or remediation. | Approved incident/action recovery policy; isolated synthetic qualification for behavior. | Unassigned — incident/action owner and recovery owner | Synthetic fixtures are sufficient for qualification; no production claims or audit rows. | Each state has a fail-safe hold/investigate/disposition process, owner, and evidence criteria. | Service and remediation-policy owner approval. |

## Submission summary

Complete this summary when submitting; reviewers update acceptance and
approval fields after review. Do not mark a submission accepted merely
because material was supplied.

- **Evidence supplied:** list item IDs, source/date, scope, and reviewer.
- **Evidence accepted after review:** list item IDs and acceptance reviewer.
- **Decisions formally approved:** list item IDs, decision, approver role,
  date, and approval reference.
- **Items still blocking Stage A:** list missing, rejected, stale, or
  insufficient evidence and the responsible role.
- **Stage A disposition:** Pending until every applicable evidence item is
  accepted and open gaps are explicitly resolved.

Stage A evidence submission does not itself authorize Stage B, production
design, implementation, backup, or restore. No submission changes the current
production **NO-GO** status without the staged approvals in the evidence plan.
