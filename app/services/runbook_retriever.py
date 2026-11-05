"""Deterministic, weighted keyword retrieval for Kybernaut runbooks."""

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

RUNBOOK_DIR = Path(__file__).resolve().parents[2] / "runbooks"
RUNBOOK_CACHE_PATH = Path(
    os.environ.get(
        "KYBERNAUT_RUNBOOK_CACHE_PATH",
        "/srv/kybernaut-data/Kybernaut/cache/application/runbook-index.json",
    )
)
RUNBOOK_CACHE_VERSION = 1

STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by",
    "for", "from", "has", "have", "how", "i", "in", "is",
    "it", "of", "on", "or", "our", "that", "the", "this",
    "to", "was", "were", "what", "when", "where", "which",
    "why", "with", "you", "your",
    "service", "issue", "problem", "investigate",
    "evidence", "observed", "check", "checks", "using",
    "used", "use", "ensure", "verify", "appropriate",
}

# These terms are more diagnostic than generic keyword matches.
IMPORTANT_TERMS = {
    "memory",
    "cpu",
    "timeout",
    "crash",
    "deployment",
    "database",
    "latency",
    "oom",
    "restart",
    "replica",
    "pod",
    "connection",
    "failure",
    "failed",
    "unavailable",
    "5xx",
}

MIN_RESOLUTION_SCORE = 0.2


class _RunbookPrerequisites(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_ready_replicas: int = Field(default=0, ge=0, le=100)
    permissions: list[Annotated[str, Field(min_length=1, max_length=40)]] = (
        Field(min_length=1, max_length=5)
    )


class _RunbookAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal[
        "restart_pod", "scale_deployment", "rollback_deployment",
        "set_image", "restore_command", "cleanup_evicted_pod",
    ]
    # Other documented actions this runbook also supports for the same evidence.
    alternatives: list[
        Literal[
            "restart_pod", "scale_deployment", "rollback_deployment",
            "set_image", "restore_command", "cleanup_evicted_pod",
        ]
    ] = Field(default_factory=list, max_length=3)
    expected_verification: str = Field(min_length=1, max_length=200)


class _RunbookContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    incident_types: list[
        Literal[
            "cpu_spike", "memory_leak", "pod_crash", "disk_full",
            "deployment_failure", "unknown",
        ]
    ] = Field(min_length=1, max_length=6)
    required_evidence: list[
        Annotated[str, Field(min_length=1, max_length=100)]
    ] = Field(min_length=1, max_length=10)
    prerequisites: _RunbookPrerequisites
    resolution: _RunbookAction
    documented_risks: list[
        Annotated[str, Field(min_length=1, max_length=300)]
    ] = Field(min_length=1, max_length=5)
    approval_required: bool


def _contract_from_document(content: str) -> _RunbookContract | None:
    if not content.startswith("---\n"):
        return None
    frontmatter, separator, _ = content[4:].partition("\n---\n")
    if not separator:
        return None
    try:
        document = yaml.safe_load(frontmatter)
        metadata = document.get("kybernaut_resolution") if isinstance(
            document, dict
        ) else None
        if not isinstance(metadata, dict):
            return None
        return _RunbookContract.model_validate(metadata)
    except (yaml.YAMLError, ValidationError, AttributeError, TypeError):
        return None


def _evidence_lines(
    evidence: list[str], matched_requirements: list[str]
) -> list[str]:
    requirement_terms = [
        set(re.findall(r"[a-z0-9]+", requirement.lower()))
        for requirement in matched_requirements
    ]
    lines = []
    for item in evidence:
        if not isinstance(item, str):
            continue
        item_terms = set(re.findall(r"[a-z0-9]+", item.lower()))
        if any(terms.issubset(item_terms) for terms in requirement_terms):
            lines.append(item[:500])
            if len(lines) == 5:
                break
    return lines


def resolve_runbook(
    query: str,
    *,
    incident_type: str,
    evidence: list[str],
    planned_action: str,
    deployment_state: dict | None,
    allow_scaled_down_restore: bool = False,
) -> dict:
    """Match a retrieved runbook contract without executing document content."""
    results = retrieve_runbooks(query, top_k=10)
    candidates = [
        item for item in results
        if isinstance(item.get("score"), (int, float))
        and item["score"] >= MIN_RESOLUTION_SCORE
    ]
    evidence_text = " ".join(item for item in evidence if isinstance(item, str))
    evidence_terms = set(re.findall(r"[a-z0-9]+", evidence_text.lower()))

    matching: list[tuple[dict, _RunbookContract, list[str]]] = []
    evidence_missing: tuple[dict, _RunbookContract] | None = None
    action_conflict: tuple[dict, _RunbookContract] | None = None

    for item in candidates:
        contract = _contract_from_document(item.get("content", ""))
        if contract is None or incident_type not in contract.incident_types:
            continue
        matched_evidence = [
            requirement
            for requirement in contract.required_evidence
            if (
                requirement_terms := set(
                    re.findall(r"[a-z0-9]+", requirement.lower())
                )
            )
            and requirement_terms.issubset(evidence_terms)
        ]
        supported = {contract.resolution.action, *contract.resolution.alternatives}
        if not matched_evidence:
            if planned_action in supported:
                evidence_missing = evidence_missing or (item, contract)
            continue
        if planned_action not in supported:
            action_conflict = action_conflict or (item, contract)
            continue
        matching.append((item, contract, matched_evidence))

    if action_conflict:
        item, contract = action_conflict
        return {
            "status": "human_review_required",
            "reason": "runbook_conflicts_with_deterministic_plan",
            "runbook_id": contract.id,
            "source": item["source"],
            "relevance_score": item["score"],
            "supporting_evidence": _evidence_lines(
                evidence,
                [
                    requirement
                    for requirement in contract.required_evidence
                    if set(re.findall(r"[a-z0-9]+", requirement.lower()))
                    .issubset(evidence_terms)
                ],
            ),
            "retrieval_terms": item.get("matched_terms", []),
            "incident_types": contract.incident_types,
            "required_evidence": contract.required_evidence,
            "matched_required_evidence": [
                requirement for requirement in contract.required_evidence
                if set(re.findall(r"[a-z0-9]+", requirement.lower()))
                .issubset(evidence_terms)
            ],
            "prerequisites": contract.prerequisites.model_dump(),
            "proposed_action": contract.resolution.action,
            "expected_verification": contract.resolution.expected_verification,
            "documented_risks": contract.documented_risks,
            "approval_required": contract.approval_required,
        }

    if len(matching) > 1:
        return {
            "status": "human_review_required",
            "reason": "multiple_applicable_runbooks",
            "runbook_id": None,
            "source": None,
            "relevance_score": None,
            "supporting_evidence": [],
            "retrieval_terms": [],
            "required_evidence": [],
            "matched_required_evidence": [],
            "prerequisites": None,
            "proposed_action": planned_action,
            "expected_verification": None,
            "documented_risks": [],
            "approval_required": True,
        }

    if matching:
        item, contract, matched_evidence = matching[0]
        details = {
            "runbook_id": contract.id,
            "source": item["source"],
            "relevance_score": item["score"],
            "supporting_evidence": _evidence_lines(
                evidence, matched_evidence
            ),
            "retrieval_terms": item.get("matched_terms", []),
            "incident_types": contract.incident_types,
            "required_evidence": contract.required_evidence,
            "matched_required_evidence": matched_evidence,
            "prerequisites": contract.prerequisites.model_dump(),
            "proposed_action": planned_action,
            "expected_verification": (
                contract.resolution.expected_verification
            ),
            "documented_risks": contract.documented_risks,
            "approval_required": contract.approval_required,
        }

        required_permissions = set(contract.prerequisites.permissions)
        if not required_permissions or not required_permissions.issubset(
            {"remediation"}
        ):
            return {
                **details,
                "status": "human_review_required",
                "reason": "unsupported_permission_requirement",
            }

        if planned_action == "cleanup_evicted_pod":
            counts = (
                deployment_state.get(key)
                if isinstance(deployment_state, dict) else None
                for key in (
                    "desired_replicas", "available_replicas", "ready_replicas"
                )
            )
            if not all(
                isinstance(value, int)
                and not isinstance(value, bool)
                and value == 1
                for value in counts
            ):
                return {
                    **details,
                    "status": "missing_prerequisites",
                    "reason": "disk_full_cleanup_requires_healthy_replica",
                    "observed_desired_replicas": (
                        deployment_state.get("desired_replicas")
                        if isinstance(deployment_state, dict) else None
                    ),
                    "observed_available_replicas": (
                        deployment_state.get("available_replicas")
                        if isinstance(deployment_state, dict) else None
                    ),
                }
            return {**details, "status": "applicable", "reason": ""}

        if planned_action in ("set_image", "restore_command", "scale_deployment"):
            # These actions remove no serving capacity (scale-out ADDS it), so
            # the "min ready replicas" prerequisite, which protects against
            # taking down the last serving pod, does not apply. They still
            # require a deployment that is meant to run (desired >= 1); scale
            # bounds and step limits are enforced separately by the policy engine.
            desired = (
                deployment_state.get("desired_replicas")
                if isinstance(deployment_state, dict) else None
            )
            if (
                not isinstance(desired, int) or isinstance(desired, bool)
                or desired < 1
            ):
                if (
                    planned_action == "restore_command"
                    and incident_type == "disk_full"
                    and allow_scaled_down_restore
                ):
                    return {**details, "status": "applicable", "reason": ""}
                return {
                    **details,
                    "status": "missing_prerequisites",
                    "reason": "deployment_scaled_down_or_unknown",
                    "observed_desired_replicas": (
                        desired if isinstance(desired, int) else None
                    ),
                }
            return {**details, "status": "applicable", "reason": ""}

        available = (
            deployment_state.get("available_replicas")
            if isinstance(deployment_state, dict) else None
        )
        minimum = contract.prerequisites.min_ready_replicas
        desired_replicas = (
            deployment_state.get("desired_replicas")
            if isinstance(deployment_state, dict) else None
        )
        # "min ready replicas" protects serving capacity. If NOTHING is serving
        # (0 available) while the deployment is meant to run (desired >= 1),
        # there is no capacity to lose, so the rule does not apply. Any state
        # with some, but too few, serving replicas is still blocked.
        nothing_serving = (
            isinstance(available, int) and not isinstance(available, bool)
            and available == 0
            and isinstance(desired_replicas, int)
            and not isinstance(desired_replicas, bool)
            and desired_replicas >= 1
        )
        if nothing_serving:
            return {**details, "status": "applicable", "reason": ""}
        if (
            not isinstance(available, int)
            or isinstance(available, bool)
            or available < minimum
        ):
            return {
                **details,
                "status": "missing_prerequisites",
                "reason": "minimum_ready_replicas_not_met",
                "observed_available_replicas": (
                    available if isinstance(available, int) else None
                ),
            }

        return {**details, "status": "applicable", "reason": ""}

    if evidence_missing:
        item, contract = evidence_missing
        return {
            "status": "insufficient_evidence",
            "reason": "required_runbook_evidence_missing",
            "runbook_id": contract.id,
            "source": item["source"],
            "relevance_score": item["score"],
            "supporting_evidence": [],
            "retrieval_terms": item.get("matched_terms", []),
            "incident_types": contract.incident_types,
            "required_evidence": contract.required_evidence,
            "matched_required_evidence": [],
            "prerequisites": contract.prerequisites.model_dump(),
            "proposed_action": contract.resolution.action,
            "expected_verification": contract.resolution.expected_verification,
            "documented_risks": contract.documented_risks,
            "approval_required": contract.approval_required,
        }

    return {
        "status": "unsupported",
        "reason": "no_applicable_runbook",
        "runbook_id": None,
        "source": None,
        "relevance_score": None,
        "supporting_evidence": [],
        "retrieval_terms": [],
        "required_evidence": [],
        "matched_required_evidence": [],
        "prerequisites": None,
        "proposed_action": None,
        "expected_verification": None,
        "documented_risks": [],
        "approval_required": None,
    }


def _tokens(text: str) -> set[str]:
    """Normalize text into searchable lowercase terms."""
    return {
        token
        for token in re.findall(r"[a-z0-9]+", text.lower())
        if token not in STOP_WORDS
    }


def _score(query_tokens: set[str], content_tokens: set[str]) -> tuple[float, list[str]]:
    """Score keyword coverage, giving important operational terms more weight."""
    matched = query_tokens & content_tokens

    if not matched:
        return 0.0, []

    total_weight = sum(
        3 if token in IMPORTANT_TERMS else 1
        for token in query_tokens
    )
    matched_weight = sum(
        3 if token in IMPORTANT_TERMS else 1
        for token in matched
    )

    score = matched_weight / total_weight if total_weight else 0.0
    return score, sorted(matched)



def _runbook_fingerprint() -> list[dict]:
    """Return stable metadata for all Markdown runbooks."""
    fingerprint = []

    for path in sorted(RUNBOOK_DIR.glob("*.md")):
        stat = path.stat()
        fingerprint.append({
            "source": path.name,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        })

    return fingerprint


def _write_runbook_index(payload: dict) -> None:
    """Atomically persist the runbook index; leave no partial cache file."""
    temporary_path = None

    try:
        RUNBOOK_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=RUNBOOK_CACHE_PATH.parent,
            prefix=".runbook-index-",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            json.dump(payload, temporary_file, ensure_ascii=False)

        # Allow the service account to read and replace the shared cache.
        os.chmod(temporary_path, 0o660)
        os.replace(temporary_path, RUNBOOK_CACHE_PATH)

    except OSError:
        # Cache storage is optional; retrieval must continue without it.
        pass

    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _load_runbook_index() -> list[dict] | None:
    """Load a valid persistent index, rebuilding it when files change."""
    default_cache_path = Path(
        "/srv/kybernaut-data/Kybernaut/cache/application/runbook-index.json"
    )

    # Never write to the underlying root filesystem if the HDD is unmounted.
    if (
        RUNBOOK_CACHE_PATH == default_cache_path
        and not Path("/srv/kybernaut-data").is_mount()
    ):
        return None

    if not RUNBOOK_DIR.is_dir():
        return None

    try:
        fingerprint = _runbook_fingerprint()
    except OSError:
        return None

    try:
        with RUNBOOK_CACHE_PATH.open("r", encoding="utf-8") as cache_file:
            cached = json.load(cache_file)

        entries = cached.get("entries")
        if (
            cached.get("version") == RUNBOOK_CACHE_VERSION
            and cached.get("fingerprint") == fingerprint
            and isinstance(entries, list)
        ):
            valid_entries = all(
                isinstance(item, dict)
                and isinstance(item.get("source"), str)
                and isinstance(item.get("content"), str)
                and isinstance(item.get("tokens"), list)
                and all(isinstance(token, str) for token in item["tokens"])
                for item in entries
            )
            if (
                valid_entries
                and [item["source"] for item in entries]
                == [item["source"] for item in fingerprint]
            ):
                try:
                    RUNBOOK_CACHE_PATH.with_name("runbook-index.last-hit").touch()
                except OSError:
                    # Activity tracking is optional; cache retrieval must continue.
                    pass
                return entries

    except (OSError, ValueError, TypeError, AttributeError):
        pass

    entries = []

    try:
        for item in fingerprint:
            runbook_path = RUNBOOK_DIR / item["source"]
            content = runbook_path.read_text(encoding="utf-8")
            entries.append({
                "source": item["source"],
                "content": content,
                "tokens": sorted(_tokens(content)),
            })
    except (OSError, UnicodeError):
        return None

    _write_runbook_index({
        "version": RUNBOOK_CACHE_VERSION,
        "fingerprint": fingerprint,
        "entries": entries,
    })

    return entries


def retrieve_runbooks(query: str, top_k: int = 3) -> list[dict]:
    """Return runbooks ranked by weighted keyword coverage."""
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 1:
        raise ValueError("top_k must be an integer greater than or equal to 1")

    if not isinstance(query, str) or not query.strip():
        return []

    if not RUNBOOK_DIR.is_dir():
        return []

    query_tokens = _tokens(query)

    if not query_tokens:
        return []

    entries = _load_runbook_index()

    if entries is None:
        entries = []
        for path in sorted(RUNBOOK_DIR.glob("*.md")):
            content = path.read_text(encoding="utf-8")
            entries.append({
                "source": path.name,
                "content": content,
                "tokens": sorted(_tokens(content)),
            })

    results = []

    for item in entries:
        content = item["content"]
        content_tokens = set(item["tokens"])

        score, matched_terms = _score(query_tokens, content_tokens)

        if not matched_terms:
            continue

        results.append({
            "source": item["source"],
            "score": score,
            "content": content,
            "matched_terms": matched_terms,
        })

    results.sort(key=lambda item: (-item["score"], item["source"]))
    return results[:top_k]