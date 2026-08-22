from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from pydantic import ValidationError

from app.schemas.finding_cluster import FindingCluster, FindingClusterStatus
from app.schemas.node import NodeRecord, NodeStatus, NodeType
from app.schemas.routing import ProjectRoutingRecord, RoutingStatus
from app.schemas.validator_attestation import (
    ValidatorAssignment,
    ValidatorAssignmentRole,
    ValidatorAssignmentStatus,
)
from app.schemas.validator_committee import (
    VALIDATOR_ASSIGNMENT_USAGE_VERSION,
    VALIDATOR_COMMITTEE_POLICY_VERSION,
    VALIDATOR_COMMITTEE_VERSION,
    ValidationAssuranceMode,
    ValidatorAssignmentUsageEvent,
    ValidatorCandidateRank,
    ValidatorCommittee,
    ValidatorCommitteeFinalizationResponse,
    ValidatorCommitteePolicyV1,
    ValidatorCommitteeSeat,
    ValidatorCommitteeStatus,
)
from app.services.finding_cluster_service import load_finding_cluster
from app.services.node_registry_service import list_nodes, load_node
from app.services.subnet_router_service import load_routing_record
from app.services.validator_assignment_service import (
    ValidatorProtocolConflictError,
    ValidatorProtocolNotFoundError,
    ValidatorProtocolRelationshipError,
    ValidatorProtocolStorageError,
    _ensure_within,
    _validate_identifier,
    build_validator_assignment_id,
    create_validator_assignment,
    get_validator_protocol_root,
    load_validator_assignment,
)
from app.utils.protocol_serialization import (
    atomic_create_json,
    atomic_write_json,
    protocol_fingerprint,
)


ACTIVE_ASSIGNMENT_STATUSES = {
    ValidatorAssignmentStatus.ASSIGNED,
    ValidatorAssignmentStatus.REPRODUCTION_RECORDED,
}


class ValidatorCommitteeError(ValidatorProtocolConflictError):
    pass


class ValidatorCommitteeInsufficientValidatorsError(ValidatorCommitteeError):
    def __init__(self, *, required: int, available: int, assurance_mode: str):
        self.required = required
        self.available = available
        self.assurance_mode = assurance_mode
        super().__init__(
            "INSUFFICIENT_VALIDATORS: "
            f"{assurance_mode} requires {required} independent authoritative "
            f"operators; {available} are eligible"
        )


class ValidatorCommitteeStaleError(ValidatorCommitteeError):
    pass


def get_committee_path(
    protocol_data_root: Path,
    routing_id: str,
    validator_committee_id: str,
) -> Path:
    _validate_identifier(routing_id, "routing")
    _validate_identifier(validator_committee_id, "validator committee")
    root = get_validator_protocol_root(protocol_data_root) / "committees" / routing_id
    path = root / f"{validator_committee_id}.json"
    _ensure_within(path, root)
    return path


def get_validator_usage_path(
    protocol_data_root: Path,
    validator_node_id: str,
    usage_event_id: str,
) -> Path:
    _validate_identifier(validator_node_id, "validator node")
    _validate_identifier(usage_event_id, "validator usage event")
    root = get_validator_protocol_root(protocol_data_root) / "usage"
    node_root = root / validator_node_id
    _ensure_within(node_root, root)
    path = node_root / f"{usage_event_id}.json"
    _ensure_within(path, node_root)
    return path


def build_validator_committee_id(
    *,
    project_id: str,
    routing_id: str,
    finding_cluster_id: str,
    validation_round: int,
    policy_version: str = VALIDATOR_COMMITTEE_POLICY_VERSION,
) -> str:
    digest = protocol_fingerprint(
        {
            "committee_version": VALIDATOR_COMMITTEE_VERSION,
            "policy_version": policy_version,
            "project_id": project_id,
            "routing_id": routing_id,
            "finding_cluster_id": finding_cluster_id,
            "validation_round": validation_round,
        }
    )
    return f"validator_committee_{digest}"


def plan_validator_committee(
    protocol_data_root: Path,
    *,
    project_id: str,
    routing_id: str,
    finding_cluster_id: str,
    assurance_mode: ValidationAssuranceMode = ValidationAssuranceMode.STANDARD,
    validation_round: int = 1,
    policy: ValidatorCommitteePolicyV1 | None = None,
    calculated_at: datetime | None = None,
    excluded_operator_ids: Iterable[str] = (),
    escalation_authorized: bool = False,
) -> ValidatorCommittee:
    """Trusted protocol planner. Membership is always derived from stored state."""
    policy = policy or ValidatorCommitteePolicyV1()
    excluded_operator_ids = tuple(sorted(set(excluded_operator_ids)))
    for operator_id in excluded_operator_ids:
        _validate_identifier(operator_id, "excluded operator")
    if validation_round != 1 and not escalation_authorized:
        raise ValidatorCommitteeError(
            "Committee selection v1 supports validation_round 1 only"
        )
    calculated_at = _as_utc(calculated_at or _utc_now())
    committee_id = build_validator_committee_id(
        project_id=project_id,
        routing_id=routing_id,
        finding_cluster_id=finding_cluster_id,
        validation_round=validation_round,
        policy_version=policy.policy_version,
    )
    existing = load_validator_committee(
        protocol_data_root, project_id, routing_id, committee_id
    )
    if existing is not None and existing.status == ValidatorCommitteeStatus.FINALIZED:
        if (
            existing.assurance_mode == assurance_mode
            and existing.policy == policy
            and existing.excluded_operator_ids == list(excluded_operator_ids)
        ):
            return existing
        raise ValidatorCommitteeError(
            "One immutable committee already exists for this cluster, round, and policy"
        )
    planned = _calculate_committee(
        protocol_data_root,
        project_id=project_id,
        routing_id=routing_id,
        finding_cluster_id=finding_cluster_id,
        assurance_mode=assurance_mode,
        validation_round=validation_round,
        policy=policy,
        committee_id=committee_id,
        calculated_at=calculated_at,
        created_at=existing.created_at if existing is not None else calculated_at,
        excluded_operator_ids=excluded_operator_ids,
    )
    path = get_committee_path(protocol_data_root, routing_id, committee_id)
    if existing is not None:
        linked = [
            assignment
            for assignment in _list_all_assignments(protocol_data_root)
            if assignment.validator_committee_id == committee_id
        ]
        if linked and existing.source_fingerprint != planned.source_fingerprint:
            raise ValidatorCommitteeError(
                "A partially materialized committee cannot be recalculated"
            )
        if existing.source_fingerprint == planned.source_fingerprint:
            return existing
        atomic_write_json(path, planned, temporary_prefix=".validator-committee-plan-")
        return planned
    if not atomic_create_json(
        path, planned, temporary_prefix=".validator-committee-create-"
    ):
        raced = load_validator_committee(
            protocol_data_root, project_id, routing_id, committee_id
        )
        if raced is None:
            raise ValidatorProtocolStorageError("Committee disappeared during creation")
        if raced.source_fingerprint != planned.source_fingerprint:
            raise ValidatorCommitteeError("A conflicting committee plan already exists")
        return raced
    return planned


def finalize_validator_committee(
    protocol_data_root: Path,
    *,
    project_id: str,
    routing_id: str,
    validator_committee_id: str,
    policy: ValidatorCommitteePolicyV1 | None = None,
) -> ValidatorCommitteeFinalizationResponse:
    committee = load_validator_committee(
        protocol_data_root, project_id, routing_id, validator_committee_id
    )
    if committee is None:
        raise ValidatorProtocolNotFoundError("Validator committee not found")
    # Round 1 keeps the Day 2 default-policy stale check. Trusted escalation
    # committees carry their narrowly scoped four-seat policy snapshot.
    policy = policy or (
        committee.policy
        if committee.validation_round > 1
        else ValidatorCommitteePolicyV1()
    )
    if committee.policy != policy:
        raise ValidatorCommitteeStaleError(
            "Validator committee policy changed; recalculate before finalization"
        )
    assignments = _load_committee_assignments(protocol_data_root, committee)
    if committee.status == ValidatorCommitteeStatus.FINALIZED:
        usage = list_validator_assignment_usage_events(
            protocol_data_root, validator_committee_id=committee.validator_committee_id
        )
        # Recover an interrupted post-finalization usage write idempotently.
        created_usage, existing_usage = _ensure_usage_events(
            protocol_data_root, committee, assignments
        )
        return ValidatorCommitteeFinalizationResponse(
            committee=committee,
            assignments_created=0,
            assignments_existing=len(assignments),
            usage_events_created=created_usage,
            usage_events_existing=max(existing_usage, len(usage)),
        )

    current = _calculate_committee(
        protocol_data_root,
        project_id=committee.project_id,
        routing_id=committee.routing_id,
        finding_cluster_id=committee.finding_cluster_id,
        assurance_mode=committee.assurance_mode,
        validation_round=committee.validation_round,
        policy=policy,
        committee_id=committee.validator_committee_id,
        calculated_at=_utc_now(),
        created_at=committee.created_at,
        excluded_operator_ids=committee.excluded_operator_ids,
    )
    if (
        current.candidate_source_fingerprint
        != committee.candidate_source_fingerprint
        or current.source_fingerprint != committee.source_fingerprint
    ):
        raise ValidatorCommitteeStaleError(
            "Validator committee source state changed; recalculate before finalization"
        )

    created_assignments = 0
    existing_assignments = 0
    assignments = []
    for seat in committee.authoritative_seats + committee.shadow_seats:
        existing = load_validator_assignment(
            protocol_data_root,
            committee.project_id,
            committee.routing_id,
            seat.validator_assignment_id,
        )
        assignment = create_validator_assignment(
            protocol_data_root,
            project_id=committee.project_id,
            routing_id=committee.routing_id,
            finding_cluster_id=committee.finding_cluster_id,
            validator_node_id=seat.validator_node_id,
            assignment_role=seat.assignment_role,
            validator_committee_id=committee.validator_committee_id,
            validation_round=committee.validation_round,
            seat_index=seat.seat_index,
        )
        if assignment.validator_assignment_id != seat.validator_assignment_id:
            raise ValidatorCommitteeError("Committee assignment identity mismatch")
        assignments.append(assignment)
        if existing is None:
            created_assignments += 1
        else:
            existing_assignments += 1

    finalized_at = _utc_now()
    finalized = committee.model_copy(
        update={
            "status": ValidatorCommitteeStatus.FINALIZED,
            "finalized_at": finalized_at,
        }
    )
    atomic_write_json(
        get_committee_path(
            protocol_data_root, committee.routing_id, committee.validator_committee_id
        ),
        finalized,
        temporary_prefix=".validator-committee-finalize-",
    )
    usage_created, usage_existing = _ensure_usage_events(
        protocol_data_root, finalized, assignments
    )
    return ValidatorCommitteeFinalizationResponse(
        committee=finalized,
        assignments_created=created_assignments,
        assignments_existing=existing_assignments,
        usage_events_created=usage_created,
        usage_events_existing=usage_existing,
    )


def load_validator_committee(
    protocol_data_root: Path,
    project_id: str,
    routing_id: str,
    validator_committee_id: str,
) -> ValidatorCommittee | None:
    _validate_identifier(project_id, "project")
    path = get_committee_path(protocol_data_root, routing_id, validator_committee_id)
    if not path.exists():
        return None
    if not path.is_file():
        raise ValidatorProtocolStorageError("Stored validator committee is not a file")
    try:
        committee = ValidatorCommittee.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
        raise ValidatorProtocolStorageError("Stored validator committee is malformed") from exc
    if (
        committee.validator_committee_id != validator_committee_id
        or committee.routing_id != routing_id
    ):
        raise ValidatorProtocolStorageError("Stored committee identity does not match its path")
    if committee.project_id != project_id:
        raise ValidatorProtocolRelationshipError("Validator committee belongs to another project")
    return committee


def list_validator_committees(
    protocol_data_root: Path,
    project_id: str,
    routing_id: str,
    *,
    finding_cluster_id: str | None = None,
) -> list[ValidatorCommittee]:
    _validate_identifier(project_id, "project")
    _validate_identifier(routing_id, "routing")
    if finding_cluster_id is not None:
        _validate_identifier(finding_cluster_id, "finding cluster")
    root = get_validator_protocol_root(protocol_data_root) / "committees" / routing_id
    if not root.exists():
        return []
    committees = []
    for path in sorted(root.glob("*.json")):
        committee = load_validator_committee(
            protocol_data_root, project_id, routing_id, path.stem
        )
        if committee is None:
            continue
        if (
            finding_cluster_id is not None
            and committee.finding_cluster_id != finding_cluster_id
        ):
            continue
        committees.append(committee)
    return sorted(
        committees,
        key=lambda item: (
            item.finding_cluster_id,
            item.validation_round,
            item.policy_version,
            item.validator_committee_id,
        ),
    )


def list_validator_assignment_usage_events(
    protocol_data_root: Path,
    *,
    validator_node_id: str | None = None,
    operator_id: str | None = None,
    validator_committee_id: str | None = None,
    role: ValidatorAssignmentRole | None = None,
) -> list[ValidatorAssignmentUsageEvent]:
    root = get_validator_protocol_root(protocol_data_root) / "usage"
    if validator_node_id is not None:
        _validate_identifier(validator_node_id, "validator node")
        paths = sorted((root / validator_node_id).glob("*.json"))
    else:
        paths = sorted(root.glob("*/*.json")) if root.exists() else []
    events = []
    for path in paths:
        try:
            event = ValidatorAssignmentUsageEvent.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
            raise ValidatorProtocolStorageError("Stored validator usage is malformed") from exc
        if path.parent.name != event.validator_node_id or path.stem != event.usage_event_id:
            raise ValidatorProtocolStorageError("Stored validator usage path is inconsistent")
        if operator_id is not None and event.operator_id != operator_id:
            continue
        if validator_committee_id is not None and event.validator_committee_id != validator_committee_id:
            continue
        if role is not None and event.role != role:
            continue
        events.append(event)
    return sorted(
        events,
        key=lambda item: (
            item.assigned_at,
            item.role.value,
            item.operator_id,
            item.validator_node_id,
            item.usage_event_id,
        ),
    )


def _calculate_committee(
    protocol_data_root: Path,
    *,
    project_id: str,
    routing_id: str,
    finding_cluster_id: str,
    assurance_mode: ValidationAssuranceMode,
    validation_round: int,
    policy: ValidatorCommitteePolicyV1,
    committee_id: str,
    calculated_at: datetime,
    created_at: datetime,
    excluded_operator_ids: Iterable[str] = (),
) -> ValidatorCommittee:
    excluded_operator_ids = set(excluded_operator_ids)
    routing, cluster = _load_context(
        protocol_data_root, project_id, routing_id, finding_cluster_id
    )
    reporting_operator_ids = _resolve_reporting_operator_ids(protocol_data_root, cluster)
    all_assignments = _list_all_assignments(protocol_data_root)
    relevant_assignments = [
        assignment
        for assignment in all_assignments
        if assignment.validator_committee_id != committee_id
    ]
    occupied_operators = {
        assignment.validator_operator_id
        for assignment in relevant_assignments
        if assignment.finding_cluster_id == cluster.finding_cluster_id
        and assignment.validation_round == validation_round
    }
    cutoff = calculated_at - timedelta(days=policy.recent_history_days)
    candidates = []
    for node in sorted(list_nodes(protocol_data_root), key=lambda item: item.node_id):
        if node.node_type not in {NodeType.VALIDATOR, NodeType.HYBRID}:
            continue
        if node.status != NodeStatus.ACTIVE:
            continue
        if cluster.category.value not in node.supported_categories:
            continue
        if (
            node.operator_id in reporting_operator_ids
            or node.operator_id in occupied_operators
            or node.operator_id in excluded_operator_ids
        ):
            continue
        candidate = _build_candidate_rank(
            node, relevant_assignments, cutoff=cutoff, now=calculated_at
        )
        if (
            candidate.current_authoritative_assignments
            + candidate.current_shadow_assignments
            >= policy.max_concurrent_validator_assignments_per_node
        ):
            continue
        candidates.append(candidate)

    groups: dict[str, list[ValidatorCandidateRank]] = defaultdict(list)
    for candidate in candidates:
        groups[candidate.operator_id].append(candidate)
    authoritative_representatives = [
        min(group, key=_representative_authoritative_key)
        for _, group in sorted(groups.items())
    ]
    authoritative_representatives.sort(key=_authoritative_key)
    authoritative_target = policy.authoritative_size(assurance_mode)
    if len(authoritative_representatives) < authoritative_target:
        raise ValidatorCommitteeInsufficientValidatorsError(
            required=authoritative_target,
            available=len(authoritative_representatives),
            assurance_mode=assurance_mode.value,
        )
    selected_authoritative = authoritative_representatives[:authoritative_target]
    selected_operators = {item.operator_id for item in selected_authoritative}

    shadow_representatives = [
        min(group, key=_shadow_key)
        for operator_id, group in sorted(groups.items())
        if operator_id not in selected_operators
    ]
    shadow_representatives.sort(key=_shadow_key)
    shadow_target = policy.shadow_slots(assurance_mode)
    selected_shadow = shadow_representatives[:shadow_target]
    if policy.shadow_slots_required and len(selected_shadow) < shadow_target:
        raise ValidatorCommitteeInsufficientValidatorsError(
            required=authoritative_target + shadow_target,
            available=len(groups),
            assurance_mode=assurance_mode.value,
        )

    authoritative_seats = _build_seats(
        project_id,
        routing_id,
        cluster.finding_cluster_id,
        validation_round,
        ValidatorAssignmentRole.AUTHORITATIVE,
        selected_authoritative,
    )
    shadow_seats = _build_seats(
        project_id,
        routing_id,
        cluster.finding_cluster_id,
        validation_round,
        ValidatorAssignmentRole.SHADOW,
        selected_shadow,
    )
    candidate_payload = {
        "committee_version": VALIDATOR_COMMITTEE_VERSION,
        "policy": policy,
        "assurance_mode": assurance_mode.value,
        "project_id": project_id,
        "routing_id": routing_id,
        "routing_source_fingerprint": routing.source_fingerprint,
        "finding_cluster_id": cluster.finding_cluster_id,
        "finding_cluster_source_fingerprint": cluster.source_fingerprint,
        "validation_round": validation_round,
        "category": cluster.category.value,
        "reporting_operator_ids": sorted(reporting_operator_ids),
        "excluded_operator_ids": sorted(excluded_operator_ids),
        "authoritative_target_size": authoritative_target,
        "shadow_target_size": shadow_target,
        "eligible_candidates": sorted(
            candidates, key=lambda item: (item.operator_id, item.validator_node_id)
        ),
    }
    candidate_fingerprint = protocol_fingerprint(candidate_payload)
    source_payload = {
        **candidate_payload,
        "candidate_source_fingerprint": candidate_fingerprint,
        "authoritative_seats": authoritative_seats,
        "shadow_seats": shadow_seats,
    }
    return ValidatorCommittee(
        validator_committee_id=committee_id,
        policy=policy,
        project_id=project_id,
        routing_id=routing_id,
        finding_cluster_id=cluster.finding_cluster_id,
        validation_round=validation_round,
        excluded_operator_ids=sorted(excluded_operator_ids),
        category=cluster.category,
        assurance_mode=assurance_mode,
        authoritative_target_size=authoritative_target,
        shadow_target_size=shadow_target,
        actual_shadow_size=len(shadow_seats),
        candidate_ranks=sorted(
            candidates, key=lambda item: (item.operator_id, item.validator_node_id)
        ),
        authoritative_seats=authoritative_seats,
        shadow_seats=shadow_seats,
        candidate_source_fingerprint=candidate_fingerprint,
        source_fingerprint=protocol_fingerprint(source_payload),
        status=ValidatorCommitteeStatus.PLANNED,
        created_at=created_at,
        calculated_at=calculated_at,
    )


def _load_context(
    protocol_data_root: Path,
    project_id: str,
    routing_id: str,
    finding_cluster_id: str,
) -> tuple[ProjectRoutingRecord, FindingCluster]:
    for value, label in (
        (project_id, "project"),
        (routing_id, "routing"),
        (finding_cluster_id, "finding cluster"),
    ):
        _validate_identifier(value, label)
    routing = load_routing_record(protocol_data_root, project_id, routing_id)
    if routing is None:
        raise ValidatorProtocolNotFoundError("Routing record not found")
    if routing.status != RoutingStatus.FINALIZED:
        raise ValidatorCommitteeError("Validator committee requires finalized routing")
    cluster = load_finding_cluster(
        protocol_data_root, project_id, routing_id, finding_cluster_id
    )
    if cluster is None:
        raise ValidatorProtocolNotFoundError("Finding cluster not found")
    if cluster.status != FindingClusterStatus.FINALIZED:
        raise ValidatorCommitteeError("Validator committee requires finalized cluster")
    if cluster.project_id != project_id or cluster.routing_id != routing_id:
        raise ValidatorProtocolRelationshipError(
            "Finding cluster does not belong to the URL project and routing"
        )
    return routing, cluster


def _resolve_reporting_operator_ids(
    protocol_data_root: Path, cluster: FindingCluster
) -> set[str]:
    operators = set()
    for member in cluster.members:
        reporter = load_node(protocol_data_root, member.node_id)
        if reporter is None:
            raise ValidatorProtocolRelationshipError(
                "Finding cluster reporter is missing from NodeRegistry"
            )
        operators.add(reporter.operator_id)
    return operators


def _build_candidate_rank(
    node: NodeRecord,
    assignments: list[ValidatorAssignment],
    *,
    cutoff: datetime,
    now: datetime,
) -> ValidatorCandidateRank:
    node_assignments = [item for item in assignments if item.validator_node_id == node.node_id]
    operator_assignments = [
        item for item in assignments if item.validator_operator_id == node.operator_id
    ]
    node_stats = _assignment_stats(node_assignments, cutoff=cutoff, now=now)
    operator_stats = _assignment_stats(operator_assignments, cutoff=cutoff, now=now)
    relevant_ids = {
        item.validator_assignment_id for item in node_assignments + operator_assignments
    }
    return ValidatorCandidateRank(
        validator_node_id=node.node_id,
        operator_id=node.operator_id,
        node_type=node.node_type,
        supported_categories=node.supported_categories,
        validator_skill_score=None,
        validator_skill_confidence=None,
        current_authoritative_assignments=node_stats["current_authoritative"],
        current_shadow_assignments=node_stats["current_shadow"],
        authoritative_assignments_last_30_days=node_stats["recent_authoritative"],
        shadow_assignments_last_30_days=node_stats["recent_shadow"],
        lifetime_authoritative_assignments=node_stats["lifetime_authoritative"],
        lifetime_shadow_assignments=node_stats["lifetime_shadow"],
        last_authoritative_assignment_at=node_stats["last_authoritative"],
        last_shadow_assignment_at=node_stats["last_shadow"],
        operator_current_authoritative_assignments=operator_stats["current_authoritative"],
        operator_current_shadow_assignments=operator_stats["current_shadow"],
        operator_authoritative_assignments_last_30_days=operator_stats["recent_authoritative"],
        operator_shadow_assignments_last_30_days=operator_stats["recent_shadow"],
        operator_lifetime_authoritative_assignments=operator_stats["lifetime_authoritative"],
        operator_lifetime_shadow_assignments=operator_stats["lifetime_shadow"],
        operator_last_authoritative_assignment_at=operator_stats["last_authoritative"],
        operator_last_shadow_assignment_at=operator_stats["last_shadow"],
        relevant_assignment_ids=sorted(relevant_ids),
        deterministic_tie_break=protocol_fingerprint(
            {"operator_id": node.operator_id, "validator_node_id": node.node_id}
        ),
    )


def _assignment_stats(
    assignments: Iterable[ValidatorAssignment],
    *,
    cutoff: datetime,
    now: datetime,
) -> dict[str, Any]:
    values = list(assignments)
    authoritative = [
        item for item in values if item.assignment_role == ValidatorAssignmentRole.AUTHORITATIVE
    ]
    shadow = [item for item in values if item.assignment_role == ValidatorAssignmentRole.SHADOW]
    active_authoritative = [item for item in authoritative if _assignment_is_active(item, now)]
    active_shadow = [item for item in shadow if _assignment_is_active(item, now)]
    return {
        "current_authoritative": len(active_authoritative),
        "current_shadow": len(active_shadow),
        "recent_authoritative": sum(item.assigned_at >= cutoff for item in authoritative),
        "recent_shadow": sum(item.assigned_at >= cutoff for item in shadow),
        "lifetime_authoritative": len(authoritative),
        "lifetime_shadow": len(shadow),
        "last_authoritative": max((item.assigned_at for item in authoritative), default=None),
        "last_shadow": max((item.assigned_at for item in shadow), default=None),
    }


def _assignment_is_active(assignment: ValidatorAssignment, now: datetime) -> bool:
    return (
        assignment.status in ACTIVE_ASSIGNMENT_STATUSES
        and (assignment.expires_at is None or assignment.expires_at > now)
    )


def _representative_authoritative_key(candidate: ValidatorCandidateRank) -> tuple[Any, ...]:
    active = candidate.current_authoritative_assignments + candidate.current_shadow_assignments
    return (
        active,
        candidate.authoritative_assignments_last_30_days,
        candidate.last_authoritative_assignment_at is not None,
        candidate.last_authoritative_assignment_at or datetime.min.replace(tzinfo=timezone.utc),
        candidate.lifetime_authoritative_assignments,
        candidate.validator_node_id,
    )


def _authoritative_key(candidate: ValidatorCandidateRank) -> tuple[Any, ...]:
    return (
        candidate.operator_current_authoritative_assignments,
        candidate.operator_authoritative_assignments_last_30_days,
        candidate.operator_last_authoritative_assignment_at is not None,
        candidate.operator_last_authoritative_assignment_at
        or datetime.min.replace(tzinfo=timezone.utc),
        candidate.operator_lifetime_authoritative_assignments,
        candidate.operator_id,
        candidate.validator_node_id,
    )


def _shadow_key(candidate: ValidatorCandidateRank) -> tuple[Any, ...]:
    return (
        candidate.operator_lifetime_authoritative_assignments != 0,
        candidate.operator_lifetime_authoritative_assignments
        + candidate.operator_lifetime_shadow_assignments,
        candidate.operator_lifetime_shadow_assignments,
        candidate.operator_last_shadow_assignment_at is not None,
        candidate.operator_last_shadow_assignment_at
        or datetime.min.replace(tzinfo=timezone.utc),
        candidate.current_authoritative_assignments + candidate.current_shadow_assignments,
        candidate.operator_id,
        candidate.validator_node_id,
    )


def _build_seats(
    project_id: str,
    routing_id: str,
    finding_cluster_id: str,
    validation_round: int,
    role: ValidatorAssignmentRole,
    candidates: list[ValidatorCandidateRank],
) -> list[ValidatorCommitteeSeat]:
    return [
        ValidatorCommitteeSeat(
            assignment_role=role,
            seat_index=index,
            validator_node_id=candidate.validator_node_id,
            operator_id=candidate.operator_id,
            validator_assignment_id=build_validator_assignment_id(
                project_id=project_id,
                routing_id=routing_id,
                finding_cluster_id=finding_cluster_id,
                validator_operator_id=candidate.operator_id,
                assignment_role=role,
                validation_round=validation_round,
            ),
        )
        for index, candidate in enumerate(candidates, start=1)
    ]


def _list_all_assignments(protocol_data_root: Path) -> list[ValidatorAssignment]:
    root = get_validator_protocol_root(protocol_data_root) / "assignments"
    assignments = []
    for path in sorted(root.glob("*/*.json")) if root.exists() else []:
        try:
            assignment = ValidatorAssignment.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
            raise ValidatorProtocolStorageError("Stored validator assignment is malformed") from exc
        if path.parent.name != assignment.routing_id or path.stem != assignment.validator_assignment_id:
            raise ValidatorProtocolStorageError("Stored validator assignment path is inconsistent")
        assignments.append(assignment)
    return sorted(
        assignments,
        key=lambda item: (
            item.assigned_at,
            item.validator_operator_id,
            item.validator_node_id,
            item.validator_assignment_id,
        ),
    )


def _load_committee_assignments(
    protocol_data_root: Path, committee: ValidatorCommittee
) -> list[ValidatorAssignment]:
    assignments = []
    for seat in committee.authoritative_seats + committee.shadow_seats:
        assignment = load_validator_assignment(
            protocol_data_root,
            committee.project_id,
            committee.routing_id,
            seat.validator_assignment_id,
        )
        if assignment is not None:
            if not (
                assignment.validator_committee_id
                == committee.validator_committee_id
                and assignment.validation_round == committee.validation_round
                and assignment.finding_cluster_id == committee.finding_cluster_id
                and assignment.validator_node_id == seat.validator_node_id
                and assignment.validator_operator_id == seat.operator_id
                and assignment.assignment_role == seat.assignment_role
                and assignment.seat_index == seat.seat_index
            ):
                raise ValidatorCommitteeError(
                    "Stored assignment does not match immutable committee membership"
                )
            assignments.append(assignment)
    return assignments


def _ensure_usage_events(
    protocol_data_root: Path,
    committee: ValidatorCommittee,
    assignments: list[ValidatorAssignment],
) -> tuple[int, int]:
    by_id = {item.validator_assignment_id: item for item in assignments}
    created = 0
    existing = 0
    for seat in committee.authoritative_seats + committee.shadow_seats:
        assignment = by_id.get(seat.validator_assignment_id)
        if assignment is None:
            raise ValidatorCommitteeError("Finalized committee assignment is missing")
        event = _build_usage_event(committee, assignment)
        path = get_validator_usage_path(
            protocol_data_root, assignment.validator_node_id, event.usage_event_id
        )
        if atomic_create_json(path, event, temporary_prefix=".validator-usage-create-"):
            created += 1
            continue
        stored = _load_usage_event(path)
        if stored != event:
            raise ValidatorCommitteeError("Conflicting validator usage event exists")
        existing += 1
    return created, existing


def _build_usage_event(
    committee: ValidatorCommittee, assignment: ValidatorAssignment
) -> ValidatorAssignmentUsageEvent:
    payload = {
        "usage_version": VALIDATOR_ASSIGNMENT_USAGE_VERSION,
        "validator_committee_id": committee.validator_committee_id,
        "committee_source_fingerprint": committee.source_fingerprint,
        "validator_assignment_id": assignment.validator_assignment_id,
        "assignment_source_fingerprint": assignment.source_fingerprint,
        "project_id": assignment.project_id,
        "routing_id": assignment.routing_id,
        "finding_cluster_id": assignment.finding_cluster_id,
        "validation_round": assignment.validation_round,
        "validator_node_id": assignment.validator_node_id,
        "operator_id": assignment.validator_operator_id,
        "category": assignment.category.value,
        "role": assignment.assignment_role.value,
    }
    return ValidatorAssignmentUsageEvent(
        usage_event_id=f"validator_usage_{assignment.validator_assignment_id}",
        validator_committee_id=committee.validator_committee_id,
        validator_assignment_id=assignment.validator_assignment_id,
        project_id=assignment.project_id,
        routing_id=assignment.routing_id,
        finding_cluster_id=assignment.finding_cluster_id,
        validation_round=assignment.validation_round,
        validator_node_id=assignment.validator_node_id,
        operator_id=assignment.validator_operator_id,
        category=assignment.category,
        role=assignment.assignment_role,
        assigned_at=assignment.assigned_at,
        source_fingerprint=protocol_fingerprint(payload),
    )


def _load_usage_event(path: Path) -> ValidatorAssignmentUsageEvent:
    try:
        return ValidatorAssignmentUsageEvent.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
        raise ValidatorProtocolStorageError("Stored validator usage is malformed") from exc


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Committee calculation time must be timezone-aware")
    return value.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
