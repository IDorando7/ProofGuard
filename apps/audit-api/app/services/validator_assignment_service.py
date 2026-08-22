from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.schemas.finding_cluster import FindingCluster, FindingClusterStatus
from app.schemas.node import NodeRecord, NodeStatus, NodeType
from app.schemas.routing import ProjectRoutingRecord, RoutingStatus
from app.schemas.validator_attestation import (
    VALIDATOR_ASSIGNMENT_VERSION,
    SAFE_PROTOCOL_IDENTIFIER,
    ValidatorAssignment,
    ValidatorAssignmentRole,
    ValidatorAssignmentStatus,
)
from app.services.finding_cluster_service import load_finding_cluster
from app.services.node_registry_service import load_node
from app.services.subnet_router_service import load_routing_record
from app.utils.protocol_serialization import (
    atomic_create_json,
    atomic_write_json,
    protocol_fingerprint,
)


class ValidatorProtocolError(ValueError):
    """Base error for Week 8 validator protocol failures."""


class ValidatorProtocolNotFoundError(ValidatorProtocolError):
    pass


class ValidatorProtocolConflictError(ValidatorProtocolError):
    pass


class ValidatorProtocolEligibilityError(ValidatorProtocolError):
    pass


class ValidatorProtocolRelationshipError(ValidatorProtocolError):
    pass


class ValidatorProtocolStorageError(ValidatorProtocolError):
    pass


def get_validator_protocol_root(protocol_data_root: Path) -> Path:
    return protocol_data_root / "validator-protocol"


def get_assignment_path(
    protocol_data_root: Path,
    routing_id: str,
    validator_assignment_id: str,
) -> Path:
    _validate_identifier(routing_id, "routing")
    _validate_identifier(validator_assignment_id, "validator assignment")
    root = get_validator_protocol_root(protocol_data_root) / "assignments" / routing_id
    path = root / f"{validator_assignment_id}.json"
    _ensure_within(path, root)
    return path


def build_assignment_source_payload(
    *,
    routing: ProjectRoutingRecord,
    cluster: FindingCluster,
    validator: NodeRecord,
    assignment_role: ValidatorAssignmentRole,
    validator_committee_id: str | None = None,
    validation_round: int = 1,
    seat_index: int | None = None,
) -> dict[str, Any]:
    return {
        "assignment_version": VALIDATOR_ASSIGNMENT_VERSION,
        "project_id": routing.project_id,
        "routing_id": routing.routing_id,
        "finding_cluster_id": cluster.finding_cluster_id,
        "finding_cluster_source_fingerprint": cluster.source_fingerprint,
        "category": cluster.category.value,
        "validator_node_id": validator.node_id,
        "validator_operator_id": validator.operator_id,
        "validator_node_type": validator.node_type.value,
        "validator_status_at_assignment": NodeStatus.ACTIVE.value,
        "validator_supported_categories": sorted(validator.supported_categories),
        "assignment_role": assignment_role.value,
        "validator_committee_id": validator_committee_id,
        "validation_round": validation_round,
        "seat_index": seat_index,
    }


def build_validator_assignment_id(
    *,
    project_id: str,
    routing_id: str,
    finding_cluster_id: str,
    validator_operator_id: str,
    assignment_role: ValidatorAssignmentRole,
    validation_round: int = 1,
) -> str:
    digest = protocol_fingerprint(
        {
            "assignment_version": VALIDATOR_ASSIGNMENT_VERSION,
            "project_id": project_id,
            "routing_id": routing_id,
            "finding_cluster_id": finding_cluster_id,
            "validator_operator_id": validator_operator_id,
            "assignment_role": assignment_role.value,
            "validation_round": validation_round,
        }
    )
    return f"validator_assignment_{digest}"


def create_validator_assignment(
    protocol_data_root: Path,
    *,
    project_id: str,
    routing_id: str,
    finding_cluster_id: str,
    validator_node_id: str,
    assignment_role: ValidatorAssignmentRole = ValidatorAssignmentRole.AUTHORITATIVE,
    expires_at: datetime | None = None,
    validator_committee_id: str | None = None,
    validation_round: int = 1,
    seat_index: int | None = None,
) -> ValidatorAssignment:
    """Trusted protocol orchestration entry point; it is intentionally not public API."""
    routing, cluster, validator = _load_authoritative_context(
        protocol_data_root,
        project_id,
        routing_id,
        finding_cluster_id,
        validator_node_id,
    )
    _ensure_no_reporting_conflict(validator, cluster)
    if validator_committee_id is not None:
        _validate_identifier(validator_committee_id, "validator committee")
        if seat_index is None or seat_index < 1:
            raise ValidatorProtocolRelationshipError(
                "Committee assignments require a positive seat index"
            )
        if cluster.category.value not in validator.supported_categories:
            raise ValidatorProtocolEligibilityError(
                "Validator does not declare support for the cluster category"
            )
    elif seat_index is not None:
        raise ValidatorProtocolRelationshipError(
            "A seat index requires a validator committee"
        )

    assignment_id = build_validator_assignment_id(
        project_id=project_id,
        routing_id=routing_id,
        finding_cluster_id=finding_cluster_id,
        validator_operator_id=validator.operator_id,
        assignment_role=assignment_role,
        validation_round=validation_round,
    )
    _ensure_operator_has_no_other_cluster_seat(
        protocol_data_root,
        project_id=project_id,
        routing_id=routing_id,
        finding_cluster_id=finding_cluster_id,
        validator_operator_id=validator.operator_id,
        validator_assignment_id=assignment_id,
    )
    payload = build_assignment_source_payload(
        routing=routing,
        cluster=cluster,
        validator=validator,
        assignment_role=assignment_role,
        validator_committee_id=validator_committee_id,
        validation_round=validation_round,
        seat_index=seat_index,
    )
    now = _utc_now()
    assignment = ValidatorAssignment(
        validator_assignment_id=assignment_id,
        project_id=project_id,
        routing_id=routing_id,
        finding_cluster_id=finding_cluster_id,
        validator_committee_id=validator_committee_id,
        validation_round=validation_round,
        validator_node_id=validator.node_id,
        validator_operator_id=validator.operator_id,
        category=cluster.category,
        assignment_role=assignment_role,
        seat_index=seat_index,
        status=ValidatorAssignmentStatus.ASSIGNED,
        source_fingerprint=protocol_fingerprint(payload),
        assigned_at=now,
        updated_at=now,
        expires_at=expires_at,
    )
    path = get_assignment_path(protocol_data_root, routing_id, assignment_id)
    if atomic_create_json(
        path,
        assignment,
        temporary_prefix=".validator-assignment-create-",
    ):
        return assignment

    existing = load_validator_assignment(
        protocol_data_root,
        project_id,
        routing_id,
        assignment_id,
    )
    if existing is None:
        raise ValidatorProtocolStorageError("Assignment disappeared during creation")
    if existing.source_fingerprint != assignment.source_fingerprint:
        raise ValidatorProtocolConflictError(
            "Validator operator already has a conflicting assignment for this cluster and role"
        )
    return existing


def load_validator_assignment(
    protocol_data_root: Path,
    project_id: str,
    routing_id: str,
    validator_assignment_id: str,
) -> ValidatorAssignment | None:
    _validate_identifier(project_id, "project")
    path = get_assignment_path(
        protocol_data_root, routing_id, validator_assignment_id
    )
    if not path.exists():
        return None
    if not path.is_file():
        raise ValidatorProtocolStorageError("Stored validator assignment is not a file")
    try:
        assignment = ValidatorAssignment.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
        raise ValidatorProtocolStorageError("Stored validator assignment is malformed") from exc
    if (
        assignment.routing_id != routing_id
        or assignment.validator_assignment_id != validator_assignment_id
    ):
        raise ValidatorProtocolStorageError("Stored assignment identity does not match its path")
    if assignment.project_id != project_id:
        raise ValidatorProtocolRelationshipError(
            "Validator assignment belongs to another project"
        )
    return assignment


def list_validator_assignments(
    protocol_data_root: Path,
    project_id: str,
    routing_id: str,
    *,
    finding_cluster_id: str | None = None,
) -> list[ValidatorAssignment]:
    _validate_identifier(project_id, "project")
    _validate_identifier(routing_id, "routing")
    if finding_cluster_id is not None:
        _validate_identifier(finding_cluster_id, "finding cluster")
    root = get_validator_protocol_root(protocol_data_root) / "assignments" / routing_id
    if not root.exists():
        return []
    assignments: list[ValidatorAssignment] = []
    for path in sorted(root.glob("*.json")):
        assignment = load_validator_assignment(
            protocol_data_root, project_id, routing_id, path.stem
        )
        if assignment is None:
            continue
        if finding_cluster_id is not None and assignment.finding_cluster_id != finding_cluster_id:
            continue
        assignments.append(assignment)
    return sorted(
        assignments,
        key=lambda item: (
            item.finding_cluster_id,
            item.assigned_at,
            item.validator_node_id,
            item.validator_assignment_id,
        ),
    )


def _ensure_validation_round_accepts_new_evidence(
    protocol_data_root: Path, assignment: ValidatorAssignment
) -> None:
    """Prevent late evidence from changing an immutably finalized round.

    The round layer is optional for legacy Day 1 assignments, so absence means
    evidence collection is still open. Keeping this check here avoids making
    Day 1/Day 3 services depend on the Day 4 consensus service.
    """
    if assignment.validator_committee_id is None:
        return
    round_id = "validation_round_" + protocol_fingerprint(
        {
            "round_version": "validation_round_v1",
            "project_id": assignment.project_id,
            "routing_id": assignment.routing_id,
            "finding_cluster_id": assignment.finding_cluster_id,
            "round_number": assignment.validation_round,
        }
    )
    path = (
        get_validator_protocol_root(protocol_data_root)
        / "rounds"
        / assignment.routing_id
        / f"{round_id}.json"
    )
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidatorProtocolStorageError(
            "Stored validation round is malformed"
        ) from exc
    if payload.get("validation_round_id") != round_id:
        raise ValidatorProtocolStorageError(
            "Stored validation round identity is inconsistent"
        )
    if payload.get("status") == "finalized":
        raise ValidatorProtocolConflictError(
            "Finalized validation round cannot accept new evidence"
        )


def validate_assignment_for_action(
    protocol_data_root: Path,
    assignment: ValidatorAssignment,
    *,
    validator_node_id: str,
    allow_reproduction_recorded: bool = True,
) -> tuple[FindingCluster, NodeRecord]:
    if assignment.validator_node_id != validator_node_id:
        raise ValidatorProtocolRelationshipError(
            "Validator node does not match the authorized assignment"
        )
    _ensure_validation_round_accepts_new_evidence(
        protocol_data_root, assignment
    )
    if assignment.status in {
        ValidatorAssignmentStatus.CANCELLED,
        ValidatorAssignmentStatus.EXPIRED,
    }:
        raise ValidatorProtocolConflictError("Validator assignment is not active")
    if assignment.status == ValidatorAssignmentStatus.ATTESTED:
        raise ValidatorProtocolConflictError("Validator assignment is already attested")
    if (
        assignment.status == ValidatorAssignmentStatus.REPRODUCTION_RECORDED
        and not allow_reproduction_recorded
    ):
        raise ValidatorProtocolConflictError("Validator reproduction is already recorded")
    if assignment.expires_at is not None and assignment.expires_at <= _utc_now():
        raise ValidatorProtocolConflictError("Validator assignment has expired")

    routing, cluster, validator = _load_authoritative_context(
        protocol_data_root,
        assignment.project_id,
        assignment.routing_id,
        assignment.finding_cluster_id,
        assignment.validator_node_id,
    )
    _ensure_no_reporting_conflict(validator, cluster)
    if validator.operator_id != assignment.validator_operator_id:
        raise ValidatorProtocolConflictError(
            "Authoritative validator operator mapping changed after assignment"
        )
    expected = protocol_fingerprint(
        build_assignment_source_payload(
            routing=routing,
            cluster=cluster,
            validator=validator,
            assignment_role=assignment.assignment_role,
            validator_committee_id=assignment.validator_committee_id,
            validation_round=assignment.validation_round,
            seat_index=assignment.seat_index,
        )
    )
    if expected != assignment.source_fingerprint:
        raise ValidatorProtocolConflictError(
            "Validator assignment source state changed"
        )
    return cluster, validator


def update_assignment_status(
    protocol_data_root: Path,
    assignment: ValidatorAssignment,
    status: ValidatorAssignmentStatus,
) -> ValidatorAssignment:
    current = load_validator_assignment(
        protocol_data_root,
        assignment.project_id,
        assignment.routing_id,
        assignment.validator_assignment_id,
    )
    if current is None:
        raise ValidatorProtocolNotFoundError("Validator assignment not found")
    if current.source_fingerprint != assignment.source_fingerprint:
        raise ValidatorProtocolConflictError("Validator assignment source changed")
    assignment = current
    if assignment.status == ValidatorAssignmentStatus.ATTESTED:
        if status == ValidatorAssignmentStatus.ATTESTED:
            return assignment
        raise ValidatorProtocolConflictError("Finalized assignment attribution is immutable")
    allowed = {
        ValidatorAssignmentStatus.ASSIGNED: {
            ValidatorAssignmentStatus.REPRODUCTION_RECORDED,
            ValidatorAssignmentStatus.ATTESTED,
            ValidatorAssignmentStatus.EXPIRED,
            ValidatorAssignmentStatus.CANCELLED,
        },
        ValidatorAssignmentStatus.REPRODUCTION_RECORDED: {
            ValidatorAssignmentStatus.ATTESTED,
            ValidatorAssignmentStatus.EXPIRED,
            ValidatorAssignmentStatus.CANCELLED,
        },
    }
    if status not in allowed.get(assignment.status, set()):
        raise ValidatorProtocolConflictError("Invalid validator assignment lifecycle transition")
    now = _utc_now()
    updated = assignment.model_copy(
        update={
            "status": status,
            "updated_at": now,
            "attested_at": now if status == ValidatorAssignmentStatus.ATTESTED else None,
        }
    )
    path = get_assignment_path(
        protocol_data_root,
        assignment.routing_id,
        assignment.validator_assignment_id,
    )
    atomic_write_json(path, updated, temporary_prefix=".validator-assignment-")
    return updated


def _load_authoritative_context(
    protocol_data_root: Path,
    project_id: str,
    routing_id: str,
    finding_cluster_id: str,
    validator_node_id: str,
) -> tuple[ProjectRoutingRecord, FindingCluster, NodeRecord]:
    for value, label in (
        (project_id, "project"),
        (routing_id, "routing"),
        (finding_cluster_id, "finding cluster"),
        (validator_node_id, "validator node"),
    ):
        _validate_identifier(value, label)
    routing = load_routing_record(protocol_data_root, project_id, routing_id)
    if routing is None:
        raise ValidatorProtocolNotFoundError("Routing record not found")
    if routing.status != RoutingStatus.FINALIZED:
        raise ValidatorProtocolConflictError("Validator assignment requires finalized routing")
    cluster = load_finding_cluster(
        protocol_data_root, project_id, routing_id, finding_cluster_id
    )
    if cluster is None:
        raise ValidatorProtocolNotFoundError("Finding cluster not found")
    if cluster.status != FindingClusterStatus.FINALIZED:
        raise ValidatorProtocolConflictError("Validator assignment requires finalized cluster")
    validator = load_node(protocol_data_root, validator_node_id)
    if validator is None:
        raise ValidatorProtocolNotFoundError("Validator node not found")
    if validator.node_type not in {NodeType.VALIDATOR, NodeType.HYBRID}:
        raise ValidatorProtocolEligibilityError(
            "Only validator or hybrid nodes may validate findings"
        )
    if validator.status != NodeStatus.ACTIVE:
        raise ValidatorProtocolEligibilityError(
            f"Validator node is not active (status: {validator.status.value})"
        )
    return routing, cluster, validator


def _ensure_no_reporting_conflict(
    validator: NodeRecord,
    cluster: FindingCluster,
) -> None:
    reporting_operators = {member.operator_id for member in cluster.members}
    if validator.operator_id in reporting_operators:
        raise ValidatorProtocolEligibilityError(
            "Validator operator cannot validate a cluster reported by that operator"
        )


def _ensure_operator_has_no_other_cluster_seat(
    protocol_data_root: Path,
    *,
    project_id: str,
    routing_id: str,
    finding_cluster_id: str,
    validator_operator_id: str,
    validator_assignment_id: str,
) -> None:
    for existing in list_validator_assignments(
        protocol_data_root,
        project_id,
        routing_id,
        finding_cluster_id=finding_cluster_id,
    ):
        if (
            existing.validator_operator_id == validator_operator_id
            and existing.validator_assignment_id != validator_assignment_id
        ):
            raise ValidatorProtocolConflictError(
                "A validator operator may occupy at most one seat for a finding cluster"
            )


def _validate_identifier(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or not SAFE_PROTOCOL_IDENTIFIER.fullmatch(value)
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or Path(value).is_absolute()
    ):
        raise ValidatorProtocolRelationshipError(f"Invalid {label} identifier")


def _ensure_within(path: Path, root: Path) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ValidatorProtocolRelationshipError("Invalid validator protocol storage path") from exc


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
