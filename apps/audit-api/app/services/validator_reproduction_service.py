from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.schemas.reproduction import ReproductionResult
from app.schemas.validator_attestation import (
    FINAL_REPRODUCTION_STATUSES,
    VALIDATOR_REPRODUCTION_VERSION,
    ValidatorAssignmentStatus,
    ValidatorReproductionRecord,
    ValidatorReproductionRequest,
)
from app.services.reproduction_service import list_reproduction_results
from app.services.reproduction_service import load_attributed_reproduction_result
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
from app.utils.protocol_serialization import atomic_create_json, protocol_fingerprint


def get_validator_reproduction_path(
    protocol_data_root: Path,
    routing_id: str,
    validator_reproduction_id: str,
) -> Path:
    _validate_identifier(routing_id, "routing")
    _validate_identifier(validator_reproduction_id, "validator reproduction")
    root = get_validator_protocol_root(protocol_data_root) / "reproductions" / routing_id
    path = root / f"{validator_reproduction_id}.json"
    _ensure_within(path, root)
    return path


def build_underlying_reproduction_fingerprint(
    result: ReproductionResult,
    *,
    poc_artifact_fingerprint: str | None,
) -> str:
    """Commit to stable Week 3 result semantics without host paths or timestamps."""
    return protocol_fingerprint(
        {
            "reproduction_id": result.reproduction_id,
            "project_id": result.project_id,
            "finding_id": result.finding_id,
            "status": result.status.value,
            "poc_artifact_fingerprint": poc_artifact_fingerprint,
            "stdout_fingerprint": (
                protocol_fingerprint(result.stdout) if result.stdout is not None else None
            ),
            "stderr_fingerprint": (
                protocol_fingerprint(result.stderr) if result.stderr is not None else None
            ),
        }
    )


def build_validator_reproduction_source_payload(
    *,
    assignment_source_fingerprint: str,
    assignment_id: str,
    project_id: str,
    routing_id: str,
    finding_cluster_id: str,
    validator_node_id: str,
    validator_operator_id: str,
    reproduction_mode: str,
    underlying_result: ReproductionResult,
    underlying_result_fingerprint: str,
    poc_artifact_fingerprint: str | None,
    environment_fingerprint: str | None,
    validator_committee_id: str | None = None,
    assignment_role: str | None = None,
    underlying_request_id: str | None = None,
    underlying_request_fingerprint: str | None = None,
    underlying_result_storage_ref: str | None = None,
    source_revision: str | None = None,
    source_snapshot_fingerprint: str | None = None,
) -> dict[str, Any]:
    payload = {
        "reproduction_protocol_version": VALIDATOR_REPRODUCTION_VERSION,
        "validator_assignment_id": assignment_id,
        "assignment_source_fingerprint": assignment_source_fingerprint,
        "project_id": project_id,
        "routing_id": routing_id,
        "finding_cluster_id": finding_cluster_id,
        "validator_node_id": validator_node_id,
        "validator_operator_id": validator_operator_id,
        "reproduction_mode": reproduction_mode,
        "underlying_reproduction_result_id": underlying_result.reproduction_id,
        "underlying_reproduction_result_fingerprint": underlying_result_fingerprint,
        "reproduction_status": underlying_result.status.value,
        "poc_artifact_fingerprint": poc_artifact_fingerprint,
        "environment_fingerprint": environment_fingerprint,
    }
    day3_fields = {
        "validator_committee_id": validator_committee_id,
        "assignment_role": assignment_role,
        "underlying_reproduction_request_id": underlying_request_id,
        "underlying_reproduction_request_fingerprint": underlying_request_fingerprint,
        "underlying_reproduction_result_storage_ref": underlying_result_storage_ref,
        "source_revision": source_revision,
        "source_snapshot_fingerprint": source_snapshot_fingerprint,
    }
    if any(value is not None for value in day3_fields.values()):
        payload.update(day3_fields)
    return payload


def create_validator_reproduction_record(
    protocol_data_root: Path,
    project_workspace: Path,
    *,
    project_id: str,
    routing_id: str,
    validator_assignment_id: str,
    request: ValidatorReproductionRequest,
) -> ValidatorReproductionRecord:
    assignment = load_validator_assignment(
        protocol_data_root, project_id, routing_id, validator_assignment_id
    )
    if assignment is None:
        raise ValidatorProtocolNotFoundError("Validator assignment not found")
    if assignment.validator_committee_id is not None:
        raise ValidatorProtocolConflictError(
            "Committee assignments require an independently executed Day 3 reproduction"
        )
    cluster, _ = validate_assignment_for_action(
        protocol_data_root,
        assignment,
        validator_node_id=request.validator_node_id,
    )
    result = _load_underlying_result(
        project_workspace, request.underlying_reproduction_result_id
    )
    if result.project_id != project_id:
        raise ValidatorProtocolRelationshipError(
            "Underlying reproduction belongs to another project"
        )
    if result.status not in FINAL_REPRODUCTION_STATUSES:
        raise ValidatorProtocolConflictError(
            "Validator reproduction requires a final Week 3 result"
        )
    cluster_finding_ids = {member.finding_id for member in cluster.members}
    if result.finding_id not in cluster_finding_ids:
        raise ValidatorProtocolRelationshipError(
            "Underlying reproduction does not belong to the assigned cluster"
        )

    artifact_fingerprint = _derive_poc_artifact_fingerprint(
        project_workspace, result
    )
    if (
        request.poc_artifact_fingerprint is not None
        and request.poc_artifact_fingerprint != artifact_fingerprint
    ):
        raise ValidatorProtocolConflictError(
            "PoC artifact fingerprint does not match authoritative stored artifact"
        )
    underlying_fingerprint = build_underlying_reproduction_fingerprint(
        result,
        poc_artifact_fingerprint=artifact_fingerprint,
    )
    identity = protocol_fingerprint(
        {
            "reproduction_protocol_version": VALIDATOR_REPRODUCTION_VERSION,
            "validator_assignment_id": assignment.validator_assignment_id,
            "reproduction_mode": request.reproduction_mode.value,
            "underlying_reproduction_result_id": result.reproduction_id,
        }
    )
    reproduction_id = f"validator_reproduction_{identity}"
    payload = build_validator_reproduction_source_payload(
        assignment_source_fingerprint=assignment.source_fingerprint,
        assignment_id=assignment.validator_assignment_id,
        project_id=project_id,
        routing_id=routing_id,
        finding_cluster_id=assignment.finding_cluster_id,
        validator_node_id=assignment.validator_node_id,
        validator_operator_id=assignment.validator_operator_id,
        reproduction_mode=request.reproduction_mode.value,
        underlying_result=result,
        underlying_result_fingerprint=underlying_fingerprint,
        poc_artifact_fingerprint=artifact_fingerprint,
        environment_fingerprint=request.environment_fingerprint,
    )
    now = _utc_now()
    record = ValidatorReproductionRecord(
        validator_reproduction_id=reproduction_id,
        validator_assignment_id=assignment.validator_assignment_id,
        project_id=project_id,
        routing_id=routing_id,
        finding_cluster_id=assignment.finding_cluster_id,
        validator_node_id=assignment.validator_node_id,
        validator_operator_id=assignment.validator_operator_id,
        reproduction_mode=request.reproduction_mode,
        underlying_reproduction_result_id=result.reproduction_id,
        underlying_reproduction_result_fingerprint=underlying_fingerprint,
        reproduction_status=result.status,
        poc_artifact_fingerprint=artifact_fingerprint,
        environment_fingerprint=request.environment_fingerprint,
        source_fingerprint=protocol_fingerprint(payload),
        created_at=now,
        finalized_at=now,
    )
    path = get_validator_reproduction_path(
        protocol_data_root, routing_id, reproduction_id
    )
    created = atomic_create_json(
        path,
        record,
        temporary_prefix=".validator-reproduction-create-",
    )
    if not created:
        existing = load_validator_reproduction_record(
            protocol_data_root, project_id, routing_id, reproduction_id
        )
        if existing is None:
            raise ValidatorProtocolStorageError(
                "Validator reproduction disappeared during creation"
            )
        if existing.source_fingerprint != record.source_fingerprint:
            raise ValidatorProtocolConflictError(
                "Conflicting validator reproduction already exists"
            )
        record = existing
    if assignment.status == ValidatorAssignmentStatus.ASSIGNED:
        update_assignment_status(
            protocol_data_root,
            assignment,
            ValidatorAssignmentStatus.REPRODUCTION_RECORDED,
        )
    return record


def load_validator_reproduction_record(
    protocol_data_root: Path,
    project_id: str,
    routing_id: str,
    validator_reproduction_id: str,
) -> ValidatorReproductionRecord | None:
    _validate_identifier(project_id, "project")
    path = get_validator_reproduction_path(
        protocol_data_root, routing_id, validator_reproduction_id
    )
    if not path.exists():
        return None
    if not path.is_file():
        raise ValidatorProtocolStorageError("Stored validator reproduction is not a file")
    try:
        record = ValidatorReproductionRecord.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
        raise ValidatorProtocolStorageError("Stored validator reproduction is malformed") from exc
    if (
        record.routing_id != routing_id
        or record.validator_reproduction_id != validator_reproduction_id
    ):
        raise ValidatorProtocolStorageError(
            "Stored validator reproduction identity does not match its path"
        )
    if record.project_id != project_id:
        raise ValidatorProtocolRelationshipError(
            "Validator reproduction belongs to another project"
        )
    return record


def validate_validator_reproduction_current(
    project_workspace: Path,
    record: ValidatorReproductionRecord,
    *,
    assignment_source_fingerprint: str,
) -> ReproductionResult:
    if record.underlying_reproduction_result_storage_ref is not None:
        result = load_attributed_reproduction_result(
            project_workspace, record.underlying_reproduction_result_storage_ref
        )
        if result is None:
            raise ValidatorProtocolNotFoundError(
                "Attributed Week 3 reproduction result not found"
            )
        if result.reproduction_id != record.underlying_reproduction_result_id:
            raise ValidatorProtocolRelationshipError(
                "Attributed Week 3 result identity does not match its record"
            )
    else:
        result = _load_underlying_result(
            project_workspace, record.underlying_reproduction_result_id
        )
    artifact_fingerprint = _derive_poc_artifact_fingerprint(project_workspace, result)
    if artifact_fingerprint != record.poc_artifact_fingerprint:
        raise ValidatorProtocolConflictError(
            "Underlying PoC artifact changed after validator reproduction"
        )
    underlying_fingerprint = build_underlying_reproduction_fingerprint(
        result,
        poc_artifact_fingerprint=artifact_fingerprint,
    )
    if underlying_fingerprint != record.underlying_reproduction_result_fingerprint:
        raise ValidatorProtocolConflictError(
            "Underlying Week 3 reproduction result changed"
        )
    payload = build_validator_reproduction_source_payload(
        assignment_source_fingerprint=assignment_source_fingerprint,
        assignment_id=record.validator_assignment_id,
        project_id=record.project_id,
        routing_id=record.routing_id,
        finding_cluster_id=record.finding_cluster_id,
        validator_node_id=record.validator_node_id,
        validator_operator_id=record.validator_operator_id,
        reproduction_mode=record.reproduction_mode.value,
        underlying_result=result,
        underlying_result_fingerprint=underlying_fingerprint,
        poc_artifact_fingerprint=artifact_fingerprint,
        environment_fingerprint=record.environment_fingerprint,
        validator_committee_id=record.validator_committee_id,
        assignment_role=(
            record.assignment_role.value if record.assignment_role is not None else None
        ),
        underlying_request_id=record.underlying_reproduction_request_id,
        underlying_request_fingerprint=(
            record.underlying_reproduction_request_fingerprint
        ),
        underlying_result_storage_ref=(
            record.underlying_reproduction_result_storage_ref
        ),
        source_revision=record.source_revision,
        source_snapshot_fingerprint=record.source_snapshot_fingerprint,
    )
    if protocol_fingerprint(payload) != record.source_fingerprint:
        raise ValidatorProtocolConflictError("Validator reproduction source changed")
    return result


def list_validator_reproduction_records(
    protocol_data_root: Path,
    project_id: str,
    routing_id: str,
    *,
    finding_cluster_id: str | None = None,
    validator_committee_id: str | None = None,
) -> list[ValidatorReproductionRecord]:
    _validate_identifier(project_id, "project")
    _validate_identifier(routing_id, "routing")
    root = get_validator_protocol_root(protocol_data_root) / "reproductions" / routing_id
    if not root.exists():
        return []
    records: list[ValidatorReproductionRecord] = []
    for path in sorted(root.glob("*.json")):
        record = load_validator_reproduction_record(
            protocol_data_root, project_id, routing_id, path.stem
        )
        if record is None:
            continue
        if finding_cluster_id is not None and record.finding_cluster_id != finding_cluster_id:
            continue
        if validator_committee_id is not None and record.validator_committee_id != validator_committee_id:
            continue
        records.append(record)
    return sorted(
        records,
        key=lambda item: (
            item.assignment_role.value if item.assignment_role is not None else "",
            item.validator_assignment_id,
            item.validator_reproduction_id,
        ),
    )


def _load_underlying_result(
    project_workspace: Path,
    reproduction_id: str,
) -> ReproductionResult:
    matches = [
        result
        for result in list_reproduction_results(project_workspace)
        if result.reproduction_id == reproduction_id
    ]
    if not matches:
        raise ValidatorProtocolNotFoundError("Underlying reproduction result not found")
    if len(matches) != 1:
        raise ValidatorProtocolStorageError(
            "Underlying reproduction result identity is not unique"
        )
    return matches[0]


def _derive_poc_artifact_fingerprint(
    project_workspace: Path,
    result: ReproductionResult,
) -> str | None:
    if result.poc_file is None:
        return None
    relative = Path(result.poc_file)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValidatorProtocolRelationshipError(
            "Underlying reproduction contains an unsafe PoC path"
        )
    repo_root = (project_workspace / "repo").resolve()
    artifact = (repo_root / relative).resolve()
    try:
        artifact.relative_to(repo_root)
    except ValueError as exc:
        raise ValidatorProtocolRelationshipError(
            "Underlying PoC artifact escapes the project repository"
        ) from exc
    if not artifact.is_file():
        raise ValidatorProtocolNotFoundError("Underlying PoC artifact not found")
    return hashlib.sha256(artifact.read_bytes()).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
