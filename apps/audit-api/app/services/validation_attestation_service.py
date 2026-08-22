from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.schemas.validator_attestation import (
    VALIDATION_ATTESTATION_VERSION,
    ValidationAttestation,
    ValidationAttestationRequest,
    ValidatorAssignmentStatus,
)
from app.services.validator_assignment_service import (
    ValidatorProtocolConflictError,
    ValidatorProtocolNotFoundError,
    ValidatorProtocolRelationshipError,
    ValidatorProtocolStorageError,
    _ensure_within,
    _validate_identifier,
    get_validator_protocol_root,
    load_validator_assignment,
    update_assignment_status,
    validate_assignment_for_action,
)
from app.services.validator_reproduction_service import (
    load_validator_reproduction_record,
    validate_validator_reproduction_current,
)
from app.services.validator_reproduction_execution_service import build_source_snapshot
from app.utils.protocol_serialization import atomic_create_json, protocol_fingerprint


def get_attestation_path(
    protocol_data_root: Path,
    routing_id: str,
    attestation_id: str,
) -> Path:
    _validate_identifier(routing_id, "routing")
    _validate_identifier(attestation_id, "attestation")
    root = get_validator_protocol_root(protocol_data_root) / "attestations" / routing_id
    path = root / f"{attestation_id}.json"
    _ensure_within(path, root)
    return path


def build_attestation_id(validator_assignment_id: str) -> str:
    digest = protocol_fingerprint(
        {
            "attestation_protocol_version": VALIDATION_ATTESTATION_VERSION,
            "validator_assignment_id": validator_assignment_id,
        }
    )
    return f"validation_attestation_{digest}"


def build_attestation_source_payload(
    *,
    assignment_source_fingerprint: str,
    reproduction_source_fingerprint: str,
    validator_assignment_id: str,
    project_id: str,
    routing_id: str,
    finding_cluster_id: str,
    validator_node_id: str,
    validator_operator_id: str,
    assignment_role: str,
    validity_decision: str,
    root_cause_decision: str,
    reproduction_decision: str,
    normalized_severity: str | None,
    impact_decision: str,
    reason_codes: list[str],
    evidence_references: list[str],
    validator_reproduction_id: str,
) -> dict[str, Any]:
    return {
        "attestation_protocol_version": VALIDATION_ATTESTATION_VERSION,
        "validator_assignment_id": validator_assignment_id,
        "assignment_source_fingerprint": assignment_source_fingerprint,
        "project_id": project_id,
        "routing_id": routing_id,
        "finding_cluster_id": finding_cluster_id,
        "validator_node_id": validator_node_id,
        "validator_operator_id": validator_operator_id,
        "assignment_role": assignment_role,
        "validity_decision": validity_decision,
        "root_cause_decision": root_cause_decision,
        "reproduction_decision": reproduction_decision,
        "normalized_severity": normalized_severity,
        "impact_decision": impact_decision,
        "reason_codes": sorted(set(reason_codes)),
        "evidence_references": sorted(set(evidence_references)),
        "validator_reproduction_id": validator_reproduction_id,
        "validator_reproduction_source_fingerprint": reproduction_source_fingerprint,
    }


def create_validation_attestation(
    protocol_data_root: Path,
    project_workspace: Path,
    *,
    project_id: str,
    routing_id: str,
    validator_assignment_id: str,
    request: ValidationAttestationRequest,
) -> ValidationAttestation:
    assignment = load_validator_assignment(
        protocol_data_root, project_id, routing_id, validator_assignment_id
    )
    if assignment is None:
        raise ValidatorProtocolNotFoundError("Validator assignment not found")
    if assignment.validator_node_id != request.validator_node_id:
        raise ValidatorProtocolRelationshipError(
            "Validator node does not match the authorized assignment"
        )

    attestation_id = build_attestation_id(assignment.validator_assignment_id)
    existing = load_validation_attestation(
        protocol_data_root, project_id, routing_id, attestation_id
    )
    if existing is not None:
        existing_reproduction = load_validator_reproduction_record(
            protocol_data_root,
            project_id,
            routing_id,
            request.validator_reproduction_id,
        )
        if existing_reproduction is None:
            raise ValidatorProtocolNotFoundError("Validator reproduction record not found")
        if (
            existing_reproduction.validator_assignment_id
            != assignment.validator_assignment_id
        ):
            raise ValidatorProtocolRelationshipError(
                "Validator reproduction does not belong to this assignment"
            )
        expected_evidence = sorted(
            set(request.evidence_references)
            | {
                assignment.validator_assignment_id,
                assignment.finding_cluster_id,
                existing_reproduction.validator_reproduction_id,
                existing_reproduction.underlying_reproduction_result_id,
            }
        )
        _validate_idempotent_request(existing, request, expected_evidence)
        if assignment.status != ValidatorAssignmentStatus.ATTESTED:
            update_assignment_status(
                protocol_data_root,
                assignment,
                ValidatorAssignmentStatus.ATTESTED,
            )
        return existing

    cluster, _ = validate_assignment_for_action(
        protocol_data_root,
        assignment,
        validator_node_id=request.validator_node_id,
    )
    reproduction = load_validator_reproduction_record(
        protocol_data_root,
        project_id,
        routing_id,
        request.validator_reproduction_id,
    )
    if reproduction is None:
        raise ValidatorProtocolNotFoundError("Validator reproduction record not found")
    if (
        reproduction.validator_assignment_id != assignment.validator_assignment_id
        or reproduction.finding_cluster_id != assignment.finding_cluster_id
        or reproduction.validator_node_id != assignment.validator_node_id
        or reproduction.validator_operator_id != assignment.validator_operator_id
    ):
        raise ValidatorProtocolRelationshipError(
            "Validator reproduction does not belong to this assignment"
        )
    if reproduction.validator_committee_id is not None and (
        reproduction.validator_committee_id != assignment.validator_committee_id
        or reproduction.assignment_role != assignment.assignment_role
    ):
        raise ValidatorProtocolRelationshipError(
            "Validator reproduction committee role does not match its assignment"
        )
    validate_validator_reproduction_current(
        project_workspace,
        reproduction,
        assignment_source_fingerprint=assignment.source_fingerprint,
    )
    if reproduction.source_snapshot_fingerprint is not None:
        _, current_source_fingerprint = build_source_snapshot(
            protocol_data_root,
            project_workspace,
            project_id=project_id,
            routing_id=routing_id,
        )
        if current_source_fingerprint != reproduction.source_snapshot_fingerprint:
            raise ValidatorProtocolConflictError(
                "Audited source snapshot changed after validator reproduction"
            )

    evidence_references = _validate_and_complete_evidence_references(
        request.evidence_references,
        assignment_id=assignment.validator_assignment_id,
        cluster=cluster,
        validator_reproduction_id=reproduction.validator_reproduction_id,
        underlying_reproduction_id=reproduction.underlying_reproduction_result_id,
    )
    payload = build_attestation_source_payload(
        assignment_source_fingerprint=assignment.source_fingerprint,
        reproduction_source_fingerprint=reproduction.source_fingerprint,
        validator_assignment_id=assignment.validator_assignment_id,
        project_id=project_id,
        routing_id=routing_id,
        finding_cluster_id=assignment.finding_cluster_id,
        validator_node_id=assignment.validator_node_id,
        validator_operator_id=assignment.validator_operator_id,
        assignment_role=assignment.assignment_role.value,
        validity_decision=request.validity_decision.value,
        root_cause_decision=request.root_cause_decision.value,
        reproduction_decision=reproduction.reproduction_status.value,
        normalized_severity=(
            request.normalized_severity.value
            if request.normalized_severity is not None
            else None
        ),
        impact_decision=request.impact_decision.value,
        reason_codes=[item.value for item in request.reason_codes],
        evidence_references=evidence_references,
        validator_reproduction_id=reproduction.validator_reproduction_id,
    )
    now = _utc_now()
    attestation = ValidationAttestation(
        attestation_id=attestation_id,
        validator_assignment_id=assignment.validator_assignment_id,
        project_id=project_id,
        routing_id=routing_id,
        finding_cluster_id=assignment.finding_cluster_id,
        validator_node_id=assignment.validator_node_id,
        validator_operator_id=assignment.validator_operator_id,
        assignment_role=assignment.assignment_role,
        validity_decision=request.validity_decision,
        root_cause_decision=request.root_cause_decision,
        reproduction_decision=reproduction.reproduction_status,
        normalized_severity=request.normalized_severity,
        impact_decision=request.impact_decision,
        reason_codes=request.reason_codes,
        evidence_references=evidence_references,
        validator_reproduction_id=reproduction.validator_reproduction_id,
        source_fingerprint=protocol_fingerprint(payload),
        created_at=now,
        finalized_at=now,
    )
    path = get_attestation_path(protocol_data_root, routing_id, attestation_id)
    created = atomic_create_json(
        path,
        attestation,
        temporary_prefix=".validation-attestation-create-",
    )
    if not created:
        raced = load_validation_attestation(
            protocol_data_root, project_id, routing_id, attestation_id
        )
        if raced is None:
            raise ValidatorProtocolStorageError(
                "Validation attestation disappeared during creation"
            )
        if raced.source_fingerprint != attestation.source_fingerprint:
            raise ValidatorProtocolConflictError(
                "A conflicting finalized attestation already exists"
            )
        attestation = raced
    update_assignment_status(
        protocol_data_root,
        assignment,
        ValidatorAssignmentStatus.ATTESTED,
    )
    return attestation


def load_validation_attestation(
    protocol_data_root: Path,
    project_id: str,
    routing_id: str,
    attestation_id: str,
) -> ValidationAttestation | None:
    _validate_identifier(project_id, "project")
    path = get_attestation_path(protocol_data_root, routing_id, attestation_id)
    if not path.exists():
        return None
    if not path.is_file():
        raise ValidatorProtocolStorageError("Stored validation attestation is not a file")
    try:
        attestation = ValidationAttestation.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
        raise ValidatorProtocolStorageError("Stored validation attestation is malformed") from exc
    if (
        attestation.routing_id != routing_id
        or attestation.attestation_id != attestation_id
    ):
        raise ValidatorProtocolStorageError(
            "Stored attestation identity does not match its path"
        )
    if attestation.project_id != project_id:
        raise ValidatorProtocolRelationshipError(
            "Validation attestation belongs to another project"
        )
    return attestation


def list_validation_attestations(
    protocol_data_root: Path,
    project_id: str,
    routing_id: str,
    *,
    finding_cluster_id: str | None = None,
) -> list[ValidationAttestation]:
    _validate_identifier(project_id, "project")
    _validate_identifier(routing_id, "routing")
    if finding_cluster_id is not None:
        _validate_identifier(finding_cluster_id, "finding cluster")
    root = get_validator_protocol_root(protocol_data_root) / "attestations" / routing_id
    if not root.exists():
        return []
    attestations: list[ValidationAttestation] = []
    for path in sorted(root.glob("*.json")):
        attestation = load_validation_attestation(
            protocol_data_root, project_id, routing_id, path.stem
        )
        if attestation is None:
            continue
        if (
            finding_cluster_id is not None
            and attestation.finding_cluster_id != finding_cluster_id
        ):
            continue
        attestations.append(attestation)
    return sorted(
        attestations,
        key=lambda item: (
            item.finding_cluster_id,
            item.validator_operator_id,
            item.validator_node_id,
            item.attestation_id,
        ),
    )


def _validate_and_complete_evidence_references(
    requested: list[str],
    *,
    assignment_id: str,
    cluster,
    validator_reproduction_id: str,
    underlying_reproduction_id: str,
) -> list[str]:
    allowed = {
        assignment_id,
        cluster.finding_cluster_id,
        validator_reproduction_id,
        underlying_reproduction_id,
    }
    for member in cluster.members:
        allowed.update(
            {
                member.submission_id,
                member.finding_id,
                member.node_id,
                member.operator_id,
                member.validation_id,
            }
        )
        if member.reproduction_id is not None:
            allowed.add(member.reproduction_id)
    invalid = sorted(set(requested) - allowed)
    if invalid:
        raise ValidatorProtocolRelationshipError(
            "Evidence references must identify authoritative assignment, cluster, member, or reproduction records"
        )
    mandatory = {
        assignment_id,
        cluster.finding_cluster_id,
        validator_reproduction_id,
        underlying_reproduction_id,
    }
    return sorted(set(requested) | mandatory)


def _validate_idempotent_request(
    existing: ValidationAttestation,
    request: ValidationAttestationRequest,
    expected_evidence_references: list[str],
) -> None:
    same = (
        existing.validator_node_id == request.validator_node_id
        and existing.validator_reproduction_id == request.validator_reproduction_id
        and existing.validity_decision == request.validity_decision
        and existing.root_cause_decision == request.root_cause_decision
        and existing.normalized_severity == request.normalized_severity
        and existing.impact_decision == request.impact_decision
        and [item.value for item in existing.reason_codes]
        == [item.value for item in request.reason_codes]
        and existing.evidence_references == expected_evidence_references
    )
    if not same:
        raise ValidatorProtocolConflictError(
            "A conflicting finalized attestation already exists for this assignment version"
        )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
