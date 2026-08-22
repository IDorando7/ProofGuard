from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from app.schemas.validator_performance import (
    VALIDATOR_PROTOCOL_VIOLATION_EVENT_VERSION,
    ValidatorProtocolViolationCode,
    ValidatorProtocolViolationEvent,
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
)
from app.utils.protocol_serialization import atomic_create_json, protocol_fingerprint


class ValidatorProtocolComplianceError(ValidatorProtocolConflictError):
    pass


def get_validator_protocol_violation_path(
    protocol_data_root: Path, routing_id: str, event_id: str
) -> Path:
    _validate_identifier(routing_id, "routing")
    _validate_identifier(event_id, "validator protocol violation")
    root = get_validator_protocol_root(protocol_data_root) / "compliance-violations" / routing_id
    path = root / f"{event_id}.json"
    _ensure_within(path, root)
    return path


def record_adjudicated_validator_protocol_violation(
    protocol_data_root: Path,
    *,
    project_id: str,
    routing_id: str,
    validator_assignment_id: str,
    violation_code: ValidatorProtocolViolationCode,
    adjudication_reference: str,
) -> ValidatorProtocolViolationEvent:
    """Trusted internal write; ordinary validator disagreement never calls this."""
    violation_code = ValidatorProtocolViolationCode(violation_code)
    _validate_identifier(adjudication_reference, "adjudication reference")
    assignment = load_validator_assignment(
        protocol_data_root, project_id, routing_id, validator_assignment_id
    )
    if assignment is None:
        raise ValidatorProtocolNotFoundError("Validator assignment not found")
    identity = {
        "event_version": VALIDATOR_PROTOCOL_VIOLATION_EVENT_VERSION,
        "validator_assignment_id": validator_assignment_id,
        "violation_code": violation_code.value,
        "adjudication_reference": adjudication_reference,
    }
    event_id = "validator_protocol_violation_" + protocol_fingerprint(identity)
    source_payload = {
        **identity,
        "project_id": assignment.project_id,
        "routing_id": assignment.routing_id,
        "finding_cluster_id": assignment.finding_cluster_id,
        "validator_node_id": assignment.validator_node_id,
        "validator_operator_id": assignment.validator_operator_id,
        "assignment_source_fingerprint": assignment.source_fingerprint,
    }
    event = ValidatorProtocolViolationEvent(
        validator_protocol_violation_event_id=event_id,
        project_id=assignment.project_id,
        routing_id=assignment.routing_id,
        finding_cluster_id=assignment.finding_cluster_id,
        validator_assignment_id=validator_assignment_id,
        validator_node_id=assignment.validator_node_id,
        validator_operator_id=assignment.validator_operator_id,
        violation_code=violation_code,
        adjudication_reference=adjudication_reference,
        source_fingerprint=protocol_fingerprint(source_payload),
        adjudicated_at=datetime.now(timezone.utc),
    )
    path = get_validator_protocol_violation_path(protocol_data_root, routing_id, event_id)
    existing = load_validator_protocol_violation(
        protocol_data_root, project_id, routing_id, event_id
    )
    if existing is not None:
        if existing.source_fingerprint != event.source_fingerprint:
            raise ValidatorProtocolComplianceError("Conflicting protocol violation exists")
        return existing
    if not atomic_create_json(path, event, temporary_prefix=".validator-violation-"):
        raced = load_validator_protocol_violation(
            protocol_data_root, project_id, routing_id, event_id
        )
        if raced is None or raced.source_fingerprint != event.source_fingerprint:
            raise ValidatorProtocolComplianceError("Conflicting protocol violation exists")
        return raced
    return event


def load_validator_protocol_violation(
    protocol_data_root: Path,
    project_id: str,
    routing_id: str,
    event_id: str,
) -> ValidatorProtocolViolationEvent | None:
    path = get_validator_protocol_violation_path(protocol_data_root, routing_id, event_id)
    if not path.exists():
        return None
    try:
        event = ValidatorProtocolViolationEvent.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
        raise ValidatorProtocolStorageError("Stored protocol violation is malformed") from exc
    if event.project_id != project_id or event.routing_id != routing_id:
        raise ValidatorProtocolRelationshipError("Protocol violation belongs to another scope")
    return event


def list_validator_protocol_violations(
    protocol_data_root: Path,
    project_id: str,
    routing_id: str,
    *,
    validator_assignment_id: str,
) -> list[ValidatorProtocolViolationEvent]:
    assignment = load_validator_assignment(
        protocol_data_root, project_id, routing_id, validator_assignment_id
    )
    if assignment is None:
        raise ValidatorProtocolNotFoundError("Validator assignment not found")
    root = get_validator_protocol_root(protocol_data_root) / "compliance-violations" / routing_id
    values = []
    for path in sorted(root.glob("*.json")) if root.exists() else []:
        event = load_validator_protocol_violation(
            protocol_data_root, project_id, routing_id, path.stem
        )
        if event and event.validator_assignment_id == validator_assignment_id:
            expected = protocol_fingerprint(
                {
                    "event_version": event.event_version,
                    "validator_assignment_id": event.validator_assignment_id,
                    "violation_code": event.violation_code.value,
                    "adjudication_reference": event.adjudication_reference,
                    "project_id": assignment.project_id,
                    "routing_id": assignment.routing_id,
                    "finding_cluster_id": assignment.finding_cluster_id,
                    "validator_node_id": assignment.validator_node_id,
                    "validator_operator_id": assignment.validator_operator_id,
                    "assignment_source_fingerprint": assignment.source_fingerprint,
                }
            )
            if (
                event.source_fingerprint != expected
                or event.validator_node_id != assignment.validator_node_id
                or event.validator_operator_id != assignment.validator_operator_id
                or event.finding_cluster_id != assignment.finding_cluster_id
            ):
                raise ValidatorProtocolComplianceError(
                    "Protocol violation attribution or fingerprint is invalid"
                )
            values.append(event)
    return sorted(values, key=lambda item: item.validator_protocol_violation_event_id)
