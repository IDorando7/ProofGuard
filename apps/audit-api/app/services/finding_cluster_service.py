from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.schemas.finding import Finding, FindingSeverity
from app.schemas.finding_cluster import (
    FINDING_CLUSTER_POLICY_VERSION,
    FINDING_CLUSTER_VERSION,
    FindingCluster,
    FindingClusterListResponse,
    FindingClusterMember,
    FindingClusterMemberRelation,
    FindingClusterRebuildResult,
    FindingClusterStatus,
    FindingClusterValidationAuthority,
)
from app.schemas.reproduction import ReproductionStatus
from app.schemas.routing import ProjectRoutingRecord, RoutingStatus
from app.schemas.scope_validation import ScopeValidationStatus
from app.schemas.submission import SubmissionRecord, SubmissionStatus
from app.schemas.validation import DuplicateKind, ValidationDecision, ValidationStatus
from app.services.deduplication_service import (
    build_dedup_key,
    build_root_cause_fingerprint,
    extract_finding_candidate,
)
from app.services.finding_service import list_project_findings
from app.services.node_registry_service import load_node, node_supports_category
from app.services.reproduction_service import load_reproduction_result
from app.services.scope_validation_service import (
    load_scope_from_project_workspace,
    validate_finding_scope,
)
from app.services.submission_service import list_project_submissions
from app.services.submission_service import compute_finding_hash
from app.services.subnet_router_service import load_routing_record
from app.services.validation_service import list_validation_decisions
from app.utils.protocol_serialization import atomic_write_json, protocol_fingerprint


CLUSTER_FILENAME = "cluster.json"
TASK_INDEX_FILENAME = "index.json"
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,255}$")


class FindingClusterServiceError(ValueError):
    pass


class FindingClusterNotFoundError(FindingClusterServiceError):
    pass


class FindingClusterRoutingNotFoundError(FindingClusterServiceError):
    pass


class FindingClusterRoutingNotFinalizedError(FindingClusterServiceError):
    pass


class FindingClusterProjectMismatchError(FindingClusterServiceError):
    pass


class FindingClusterInputError(FindingClusterServiceError):
    pass


class FindingClusterFinalizedError(FindingClusterServiceError):
    pass


class FindingClusterStorageError(FindingClusterServiceError):
    pass


@dataclass(frozen=True)
class _EligibleSource:
    submission: SubmissionRecord
    finding: Finding
    validation: ValidationDecision | None
    reproduction_id: str | None
    reproduced: bool
    operator_id: str
    anchor_finding_id: str
    root_cause_fingerprint: str


def get_finding_clusters_root(protocol_data_root: Path) -> Path:
    return protocol_data_root / "finding-clusters" / "tasks"


def get_task_clusters_root(protocol_data_root: Path, routing_id: str) -> Path:
    _validate_identifier(routing_id, "routing")
    root = get_finding_clusters_root(protocol_data_root)
    task_root = root / routing_id
    _ensure_within(task_root, root)
    return task_root


def get_cluster_path(
    protocol_data_root: Path,
    routing_id: str,
    finding_cluster_id: str,
) -> Path:
    _validate_identifier(finding_cluster_id, "finding cluster")
    task_root = get_task_clusters_root(protocol_data_root, routing_id)
    path = task_root / finding_cluster_id / CLUSTER_FILENAME
    _ensure_within(path, task_root)
    return path


def build_finding_cluster_id(
    project_id: str,
    routing_id: str,
    category: str,
    root_cause_fingerprint: str,
) -> str:
    identity = protocol_fingerprint(
        {
            "cluster_version": FINDING_CLUSTER_VERSION,
            "project_id": project_id,
            "routing_id": routing_id,
            "category": category,
            "root_cause_fingerprint": root_cause_fingerprint,
        }
    )
    return f"finding_cluster_{identity}"


def build_member_source_payload(
    submission: SubmissionRecord,
    operator_id: str,
    validation: ValidationDecision | None,
    reproduction_id: str | None,
    relation: FindingClusterMemberRelation,
) -> dict[str, Any]:
    return {
        "submission_id": submission.submission_id,
        "finding_id": submission.finding_id,
        "node_id": submission.node_id,
        "operator_id": operator_id,
        "validation_id": validation.validation_id if validation is not None else None,
        "reproduction_id": reproduction_id,
        "submitted_at": submission.submitted_at,
        "relation": relation.value,
    }


def build_cluster_source_payload(
    *,
    project_id: str,
    routing_id: str,
    category: str,
    root_cause_key: str,
    root_cause_fingerprint: str,
    canonical_finding_id: str,
    canonical_submission_id: str,
    claimed_severity: FindingSeverity,
    final_validation_status: ValidationStatus | None,
    final_severity: FindingSeverity | None,
    validation_authority: FindingClusterValidationAuthority,
    members: list[FindingClusterMember],
) -> dict[str, Any]:
    payload = {
        "cluster_version": FINDING_CLUSTER_VERSION,
        "policy_version": FINDING_CLUSTER_POLICY_VERSION,
        "dedup_policy_version": "deterministic_field_similarity_v0",
        "project_id": project_id,
        "routing_id": routing_id,
        "category": category,
        "root_cause_key": root_cause_key,
        "root_cause_fingerprint": root_cause_fingerprint,
        "canonical_finding_id": canonical_finding_id,
        "canonical_submission_id": canonical_submission_id,
        "members": [
            {
                "submission_id": member.submission_id,
                "finding_id": member.finding_id,
                "node_id": member.node_id,
                "operator_id": member.operator_id,
                "validation_id": member.validation_id,
                "reproduction_id": member.reproduction_id,
                "submitted_at": member.submitted_at,
                "relation": member.relation.value,
                "source_fingerprint": member.source_fingerprint,
            }
            for member in members
        ],
    }
    if validation_authority == FindingClusterValidationAuthority.LEGACY_BACKEND:
        # Preserve the v1 fingerprint of already-reproduced legacy clusters.
        payload.update(
            {
                "final_validation_status": ValidationStatus.ACCEPTED.value,
                "final_severity": final_severity.value if final_severity else None,
            }
        )
    else:
        payload.update(
            {
                "claimed_severity": claimed_severity.value,
                "final_validation_status": (
                    final_validation_status.value
                    if final_validation_status is not None
                    else None
                ),
                "final_severity": (
                    final_severity.value if final_severity is not None else None
                ),
                "validation_authority": validation_authority.value,
            }
        )
    return payload


def rebuild_finding_clusters_for_task(
    protocol_data_root: Path,
    project_workspace: Path,
    project_id: str,
    routing_id: str,
) -> FindingClusterRebuildResult:
    routing = _load_task_routing(protocol_data_root, project_id, routing_id)
    findings = {item.finding_id: item for item in list_project_findings(project_workspace)}
    validations = {
        item.finding_id: item for item in list_validation_decisions(project_workspace)
    }
    eligible = _collect_eligible_sources(
        protocol_data_root,
        project_workspace,
        project_id,
        routing,
        findings,
        validations,
    )

    grouped: dict[tuple[str, str], list[_EligibleSource]] = {}
    for source in eligible:
        grouped.setdefault(
            (source.submission.category, source.root_cause_fingerprint), []
        ).append(source)

    clusters: list[FindingCluster] = []
    created = 0
    updated = 0
    unchanged = 0
    for (category, root_cause_fingerprint), sources in sorted(grouped.items()):
        anchor_id = min(
            sources,
            key=lambda item: (
                0 if item.reproduced else 1,
                item.submission.submitted_at,
                item.finding.finding_id,
                item.submission.submission_id,
            ),
        ).anchor_finding_id
        anchor_finding = findings.get(anchor_id)
        if anchor_finding is None:
            raise FindingClusterInputError(
                f"Canonical dedup finding '{anchor_id}' is missing"
            )
        if anchor_finding.category.value != category:
            raise FindingClusterInputError(
                "Dedup relation cannot cross vulnerability categories"
            )
        if build_root_cause_fingerprint(anchor_finding) != root_cause_fingerprint:
            raise FindingClusterInputError(
                "Root-cause group does not match its canonical finding"
            )
        cluster, state = _materialize_cluster(
            protocol_data_root,
            project_id,
            routing_id,
            category,
            anchor_finding,
            sources,
        )
        clusters.append(cluster)
        if state == "created":
            created += 1
        elif state == "updated":
            updated += 1
        else:
            unchanged += 1

    clusters.sort(key=lambda item: item.finding_cluster_id)
    _write_task_index_if_changed(
        protocol_data_root,
        project_id,
        routing_id,
        clusters,
    )
    return FindingClusterRebuildResult(
        project_id=project_id,
        routing_id=routing_id,
        total_clusters=len(clusters),
        created_clusters=created,
        updated_clusters=updated,
        unchanged_clusters=unchanged,
        clusters=clusters,
    )


def list_task_finding_clusters(
    protocol_data_root: Path,
    project_id: str,
    routing_id: str,
) -> list[FindingCluster]:
    _validate_identifier(project_id, "project")
    index = _load_task_index(protocol_data_root, routing_id)
    if index is None:
        return []
    if index.get("project_id") != project_id or index.get("routing_id") != routing_id:
        raise FindingClusterProjectMismatchError(
            "Finding cluster task does not belong to the requested project"
        )
    cluster_ids = index.get("cluster_ids")
    if not isinstance(cluster_ids, list) or cluster_ids != sorted(cluster_ids):
        raise FindingClusterStorageError("Stored finding cluster index is malformed")
    clusters = [
        _load_cluster_file(protocol_data_root, routing_id, cluster_id)
        for cluster_id in cluster_ids
    ]
    if any(cluster is None for cluster in clusters):
        raise FindingClusterStorageError("Finding cluster index references a missing cluster")
    output = [cluster for cluster in clusters if cluster is not None]
    if any(cluster.project_id != project_id for cluster in output):
        raise FindingClusterProjectMismatchError(
            "Stored cluster project does not match its task index"
        )
    return output


def load_finding_cluster(
    protocol_data_root: Path,
    project_id: str,
    routing_id: str,
    finding_cluster_id: str,
) -> FindingCluster | None:
    current = {
        cluster.finding_cluster_id: cluster
        for cluster in list_task_finding_clusters(
            protocol_data_root, project_id, routing_id
        )
    }
    return current.get(finding_cluster_id)


def attach_final_validator_consensus(
    protocol_data_root: Path,
    *,
    project_id: str,
    routing_id: str,
    finding_cluster_id: str,
    validation_consensus_id: str,
    consensus_outcome: str,
    consensus_severity: FindingSeverity | None,
    consensus_source_fingerprint: str,
) -> FindingCluster:
    """Attach new Week 8 authority without rewriting legacy cluster evidence."""
    cluster = load_finding_cluster(
        protocol_data_root, project_id, routing_id, finding_cluster_id
    )
    if cluster is None:
        raise FindingClusterNotFoundError("Finding cluster not found")
    if cluster.final_validation_consensus_id is not None:
        if (
            cluster.final_validation_consensus_id == validation_consensus_id
            and cluster.validator_consensus_outcome == consensus_outcome
            and cluster.validator_consensus_severity == consensus_severity
            and cluster.validation_resolution_source_fingerprint
            == consensus_source_fingerprint
        ):
            return cluster
        raise FindingClusterFinalizedError(
            "Finding cluster already references a different finalized validator consensus"
        )
    final_status = {
        "confirmed": ValidationStatus.ACCEPTED,
        "rejected": ValidationStatus.REJECTED,
        "out_of_scope": ValidationStatus.OUT_OF_SCOPE,
        "insufficient_evidence": ValidationStatus.INSUFFICIENT_EVIDENCE,
        "unsafe": ValidationStatus.UNSAFE_POC,
        "unsupported": ValidationStatus.UNSUPPORTED,
    }.get(consensus_outcome)
    if final_status is None:
        raise FindingClusterInputError(
            "Only resolved validator consensus may be attached to a cluster"
        )
    updated = FindingCluster.model_validate(
        {
            **cluster.model_dump(),
            "validation_authority": FindingClusterValidationAuthority.VALIDATOR_CONSENSUS,
            "final_validation_status": final_status,
            "final_severity": (
                consensus_severity
                if final_status == ValidationStatus.ACCEPTED
                else None
            ),
            "final_validation_consensus_id": validation_consensus_id,
            "validator_consensus_outcome": consensus_outcome,
            "validator_consensus_severity": (
                consensus_severity
                if final_status == ValidationStatus.ACCEPTED
                else None
            ),
            "validation_resolution_source_fingerprint": consensus_source_fingerprint,
            "updated_at": _utc_now(),
        }
    )
    atomic_write_json(
        get_cluster_path(protocol_data_root, routing_id, finding_cluster_id),
        updated,
        temporary_prefix=".finding-cluster-consensus-",
    )
    return updated


def find_cluster_by_submission(
    protocol_data_root: Path,
    submission_id: str,
) -> FindingCluster | None:
    _validate_identifier(submission_id, "submission")
    root = get_finding_clusters_root(protocol_data_root)
    if not root.exists():
        return None
    matches: list[FindingCluster] = []
    for index_path in sorted(root.glob(f"*/{TASK_INDEX_FILENAME}")):
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
            project_id = index["project_id"]
            routing_id = index["routing_id"]
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError) as exc:
            raise FindingClusterStorageError("Stored finding cluster index is malformed") from exc
        for cluster in list_task_finding_clusters(
            protocol_data_root, project_id, routing_id
        ):
            if any(member.submission_id == submission_id for member in cluster.members):
                matches.append(cluster)
    if len(matches) > 1:
        raise FindingClusterStorageError(
            "A submission is referenced by multiple current finding clusters"
        )
    return matches[0] if matches else None


def finalize_finding_clusters_for_task(
    protocol_data_root: Path,
    project_id: str,
    routing_id: str,
) -> list[FindingCluster]:
    clusters = list_task_finding_clusters(protocol_data_root, project_id, routing_id)
    now = _utc_now()
    finalized: list[FindingCluster] = []
    for cluster in clusters:
        if cluster.status == FindingClusterStatus.FINALIZED:
            finalized.append(cluster)
            continue
        updated = FindingCluster.model_validate(
            {
                **cluster.model_dump(),
                "status": FindingClusterStatus.FINALIZED,
                "updated_at": now,
                "finalized_at": now,
            }
        )
        atomic_write_json(
            get_cluster_path(
                protocol_data_root, routing_id, cluster.finding_cluster_id
            ),
            updated,
            temporary_prefix=".finding-cluster-",
        )
        finalized.append(updated)
    return finalized


def _collect_eligible_sources(
    protocol_data_root: Path,
    project_workspace: Path,
    project_id: str,
    routing: ProjectRoutingRecord,
    findings: dict[str, Finding],
    validations: dict[str, ValidationDecision],
) -> list[_EligibleSource]:
    scope = load_scope_from_project_workspace(project_workspace)
    assignments = {
        assignment.assignment_id: assignment
        for result in routing.results
        for assignment in result.assignments
    }
    candidates: list[_EligibleSource] = []
    for submission in list_project_submissions(protocol_data_root, project_id):
        if submission.project_id != project_id:
            raise FindingClusterInputError(
                "Submission does not belong to the requested project"
            )
        if submission.routing_id != routing.routing_id:
            continue
        if submission.status in {
            SubmissionStatus.REJECTED,
            SubmissionStatus.DUPLICATE,
            SubmissionStatus.OUT_OF_SCOPE,
            SubmissionStatus.INSUFFICIENT_EVIDENCE,
            SubmissionStatus.UNSAFE,
            SubmissionStatus.UNSUPPORTED,
            SubmissionStatus.PENALIZED,
        }:
            continue
        assignment = assignments.get(submission.routing_assignment_id or "")
        if assignment is None:
            raise FindingClusterInputError(
                "Submission routing assignment is missing from its finalized task"
            )
        if (
            assignment.project_id != project_id
            or assignment.node_id != submission.node_id
            or assignment.category.value != submission.category
        ):
            raise FindingClusterInputError(
                "Submission does not match its finalized routing assignment"
            )
        finding = findings.get(submission.finding_id)
        validation = validations.get(submission.finding_id)
        if finding is None:
            raise FindingClusterInputError(
                "Routed submission references a missing finding"
            )
        if compute_finding_hash(project_id, finding) != submission.finding_hash:
            raise FindingClusterInputError(
                "Routed submission finding content changed after submission"
            )
        if finding.project_id != project_id or finding.category.value != submission.category:
            raise FindingClusterInputError("Finding identity does not match submission")
        scope_result = validate_finding_scope(finding, scope)
        if validation is not None and (
            validation.project_id != project_id
            or validation.finding_id != finding.finding_id
        ):
            raise FindingClusterInputError("Validation identity does not match finding")
        if validation is not None and submission.validation_id not in {
            None,
            validation.validation_id,
        }:
            raise FindingClusterInputError("Submission validation reference does not match")
        if validation is not None and validation.status in {
            ValidationStatus.REJECTED,
            ValidationStatus.DUPLICATE,
            ValidationStatus.OUT_OF_SCOPE,
            ValidationStatus.INSUFFICIENT_EVIDENCE,
            ValidationStatus.UNSAFE_POC,
            ValidationStatus.UNSUPPORTED,
        }:
            continue
        evidence = validation.evidence if validation is not None else None
        reproduction = load_reproduction_result(project_workspace, finding.finding_id)
        reproduced = (
            reproduction is not None
            and reproduction.status == ReproductionStatus.REPRODUCED
        )
        legacy_accepted = (
            validation is not None
            and validation.status == ValidationStatus.ACCEPTED
            and reproduced
        )
        if (
            not legacy_accepted
            and scope_result.status != ScopeValidationStatus.IN_SCOPE
        ):
            # Candidate admission must satisfy the current preliminary scope.
            # A genuine legacy ACCEPTED+REPRODUCED source remains readable even
            # when imported historical fixtures predate strict scope linkage.
            continue
        independent = (
            legacy_accepted
            and evidence is not None
            and evidence.duplicate_kind == DuplicateKind.INDEPENDENT_ROOT_CAUSE
            and evidence.is_valid_duplicate is True
            and evidence.is_duplicate is True
            and bool(evidence.canonical_finding_id)
        )
        if legacy_accepted and evidence is not None and evidence.is_duplicate is True and not independent:
            # Historical duplicate decisions are intentionally not guessed valid.
            continue
        if submission.reproduction_id is not None and (
            reproduction is None
            or submission.reproduction_id != reproduction.reproduction_id
        ):
            raise FindingClusterInputError("Submission reproduction reference does not match")
        node = load_node(protocol_data_root, submission.node_id)
        if node is None or not node.operator_id:
            raise FindingClusterInputError(
                f"Operator identity is missing for node '{submission.node_id}'"
            )
        if node.node_type.value not in {"agent", "hybrid"}:
            raise FindingClusterInputError(
                "Only agent-capable routed nodes may source finding clusters"
            )
        if not node_supports_category(node, submission.category):
            raise FindingClusterInputError(
                "Submission category is no longer compatible with its source node"
            )
        anchor = (
            evidence.canonical_finding_id
            if independent and evidence is not None and evidence.canonical_finding_id
            else finding.finding_id
        )
        anchor = _resolve_anchor_finding_id(
            anchor,
            submission.category,
            findings,
            validations,
        )
        anchor_finding = findings.get(anchor)
        if anchor_finding is None:
            raise FindingClusterInputError(
                f"Canonical dedup finding '{anchor}' is missing"
            )
        candidates.append(
            _EligibleSource(
                submission=submission,
                finding=finding,
                validation=validation,
                # Legacy accepted stores may have dropped the submission-level
                # reference during their final status transition. Preserve their
                # reproduced evidence, while keeping a later merely-generated
                # validator artifact external to immutable cluster membership.
                reproduction_id=(
                    reproduction.reproduction_id
                    if legacy_accepted and reproduction is not None
                    else submission.reproduction_id
                ),
                reproduced=reproduced,
                operator_id=node.operator_id,
                anchor_finding_id=anchor,
                root_cause_fingerprint=build_root_cause_fingerprint(anchor_finding),
            )
        )

    # Defense in depth for imported legacy stores: same-node retries never count.
    by_spam_key: dict[tuple[str, str], _EligibleSource] = {}
    for source in sorted(
        candidates,
        key=lambda item: (
            item.submission.submitted_at,
            item.submission.submission_id,
        ),
    ):
        by_spam_key.setdefault(
            (source.submission.node_id, source.submission.finding_hash), source
        )
    return list(by_spam_key.values())


def _resolve_anchor_finding_id(
    finding_id: str,
    category: str,
    findings: dict[str, Finding],
    validations: dict[str, ValidationDecision],
) -> str:
    current = finding_id
    visited: set[str] = set()
    while current not in visited:
        visited.add(current)
        finding = findings.get(current)
        if finding is None:
            return current
        if finding.category.value != category:
            raise FindingClusterInputError(
                "Dedup relation cannot cross vulnerability categories"
            )
        validation = validations.get(current)
        if validation is None:
            return current
        evidence = validation.evidence
        if (
            evidence.duplicate_kind != DuplicateKind.INDEPENDENT_ROOT_CAUSE
            or evidence.is_valid_duplicate is not True
            or not evidence.canonical_finding_id
        ):
            return current
        current = evidence.canonical_finding_id
    raise FindingClusterInputError("Cyclic canonical dedup relation detected")


def _materialize_cluster(
    protocol_data_root: Path,
    project_id: str,
    routing_id: str,
    category: str,
    anchor_finding: Finding,
    sources: list[_EligibleSource],
) -> tuple[FindingCluster, str]:
    canonical_source = _select_canonical_source(anchor_finding.finding_id, sources)
    claimed_severity = canonical_source.finding.severity
    legacy_authoritative = (
        canonical_source.reproduced
        and canonical_source.validation is not None
        and canonical_source.validation.status == ValidationStatus.ACCEPTED
    )
    final_validation_status = (
        ValidationStatus.ACCEPTED if legacy_authoritative else None
    )
    validation_authority = (
        FindingClusterValidationAuthority.LEGACY_BACKEND
        if legacy_authoritative
        else FindingClusterValidationAuthority.PENDING_VALIDATOR_CONSENSUS
    )
    final_severity = None
    if legacy_authoritative:
        severity_value = canonical_source.validation.evidence.normalized_severity
        try:
            final_severity = FindingSeverity(severity_value)
        except (TypeError, ValueError) as exc:
            raise FindingClusterInputError(
                "Canonical validator-approved normalized severity is required"
            ) from exc

    ordered_sources = sorted(
        sources,
        key=lambda item: (
            item.submission.submitted_at,
            item.submission.submission_id,
        ),
    )
    members: list[FindingClusterMember] = []
    for source in ordered_sources:
        relation = (
            FindingClusterMemberRelation.CANONICAL
            if source.submission.submission_id
            == canonical_source.submission.submission_id
            else FindingClusterMemberRelation.INDEPENDENT_DUPLICATE
        )
        member_payload = build_member_source_payload(
            source.submission,
            source.operator_id,
            source.validation,
            source.reproduction_id,
            relation,
        )
        members.append(
            FindingClusterMember(
                **member_payload,
                source_fingerprint=protocol_fingerprint(member_payload),
            )
        )

    anchor_candidate = extract_finding_candidate(anchor_finding)
    root_cause_key = build_dedup_key(anchor_candidate)
    root_cause_fingerprint = build_root_cause_fingerprint(anchor_candidate)
    cluster_id = build_finding_cluster_id(
        project_id, routing_id, category, root_cause_fingerprint
    )
    source_payload = build_cluster_source_payload(
        project_id=project_id,
        routing_id=routing_id,
        category=category,
        root_cause_key=root_cause_key,
        root_cause_fingerprint=root_cause_fingerprint,
        canonical_finding_id=canonical_source.finding.finding_id,
        canonical_submission_id=canonical_source.submission.submission_id,
        claimed_severity=claimed_severity,
        final_validation_status=final_validation_status,
        final_severity=final_severity,
        validation_authority=validation_authority,
        members=members,
    )
    source_fingerprint = protocol_fingerprint(source_payload)
    existing = _load_cluster_file(protocol_data_root, routing_id, cluster_id)
    if existing is not None and existing.source_fingerprint == source_fingerprint:
        return existing, "unchanged"
    if existing is not None and existing.status == FindingClusterStatus.FINALIZED:
        raise FindingClusterFinalizedError(
            "Finalized finding cluster source cannot be changed"
        )
    now = _utc_now()
    cluster = FindingCluster(
        finding_cluster_id=cluster_id,
        project_id=project_id,
        routing_id=routing_id,
        category=category,
        canonical_finding_id=canonical_source.finding.finding_id,
        canonical_submission_id=canonical_source.submission.submission_id,
        final_validation_status=final_validation_status,
        claimed_severity=claimed_severity,
        final_severity=final_severity,
        validation_authority=validation_authority,
        root_cause_key=root_cause_key,
        root_cause_fingerprint=root_cause_fingerprint,
        members=members,
        report_count=len(members),
        distinct_operator_count=len({member.operator_id for member in members}),
        status=FindingClusterStatus.OPEN,
        source_fingerprint=source_fingerprint,
        created_at=existing.created_at if existing is not None else now,
        updated_at=now,
        finalized_at=None,
    )
    atomic_write_json(
        get_cluster_path(protocol_data_root, routing_id, cluster_id),
        cluster,
        temporary_prefix=".finding-cluster-",
    )
    return cluster, "updated" if existing is not None else "created"


def _select_canonical_source(
    preferred_finding_id: str,
    sources: list[_EligibleSource],
) -> _EligibleSource:
    preferred = [
        source for source in sources if source.finding.finding_id == preferred_finding_id
    ]
    candidates = preferred or sources
    return min(
        candidates,
        key=lambda source: (
            0 if source.reproduced else 1,
            source.submission.submitted_at,
            source.finding.finding_id,
            source.submission.submission_id,
        ),
    )


def _load_task_routing(
    protocol_data_root: Path,
    project_id: str,
    routing_id: str,
) -> ProjectRoutingRecord:
    _validate_identifier(project_id, "project")
    _validate_identifier(routing_id, "routing")
    routing = load_routing_record(protocol_data_root, project_id, routing_id)
    if routing is None:
        raise FindingClusterRoutingNotFoundError("Routing record not found")
    if routing.project_id != project_id:
        raise FindingClusterProjectMismatchError(
            "Routing does not belong to the requested project"
        )
    if routing.status != RoutingStatus.FINALIZED:
        raise FindingClusterRoutingNotFinalizedError(
            "Finding clusters require a finalized routing task"
        )
    return routing


def _load_cluster_file(
    protocol_data_root: Path,
    routing_id: str,
    finding_cluster_id: str,
) -> FindingCluster | None:
    path = get_cluster_path(protocol_data_root, routing_id, finding_cluster_id)
    if not path.exists():
        return None
    if not path.is_file():
        raise FindingClusterStorageError("Stored finding cluster is not a regular file")
    try:
        return FindingCluster.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
        raise FindingClusterStorageError("Stored finding cluster is malformed") from exc


def _task_index_payload(
    project_id: str,
    routing_id: str,
    clusters: list[FindingCluster],
) -> dict[str, Any]:
    sources = [
        {
            "finding_cluster_id": cluster.finding_cluster_id,
            "source_fingerprint": cluster.source_fingerprint,
        }
        for cluster in clusters
    ]
    return {
        "cluster_version": FINDING_CLUSTER_VERSION,
        "project_id": project_id,
        "routing_id": routing_id,
        "cluster_ids": [item["finding_cluster_id"] for item in sources],
        "cluster_sources": sources,
        "source_fingerprint": protocol_fingerprint(sources),
    }


def _write_task_index_if_changed(
    protocol_data_root: Path,
    project_id: str,
    routing_id: str,
    clusters: list[FindingCluster],
) -> None:
    payload = _task_index_payload(project_id, routing_id, clusters)
    existing = _load_task_index(protocol_data_root, routing_id)
    if existing == payload:
        return
    atomic_write_json(
        get_task_clusters_root(protocol_data_root, routing_id) / TASK_INDEX_FILENAME,
        payload,
        temporary_prefix=".finding-cluster-index-",
    )


def _load_task_index(
    protocol_data_root: Path,
    routing_id: str,
) -> dict[str, Any] | None:
    path = get_task_clusters_root(protocol_data_root, routing_id) / TASK_INDEX_FILENAME
    if not path.exists():
        return None
    if not path.is_file():
        raise FindingClusterStorageError("Stored finding cluster index is not a file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FindingClusterStorageError("Stored finding cluster index is malformed") from exc
    if not isinstance(payload, dict):
        raise FindingClusterStorageError("Stored finding cluster index is malformed")
    return payload


def _validate_identifier(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or not SAFE_IDENTIFIER.fullmatch(value)
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or Path(value).is_absolute()
    ):
        raise FindingClusterInputError(f"Invalid {label} identifier")


def _ensure_within(path: Path, root: Path) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise FindingClusterInputError("Invalid finding cluster storage path") from exc


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
