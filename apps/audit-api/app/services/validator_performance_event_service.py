from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from app.schemas.validator_performance import (
    VALIDATOR_PERFORMANCE_EVENT_VERSION,
    ValidationQualityAssessmentStatus,
    ValidatorPerformanceEvent,
)
from app.services.validation_quality_assessment_service import (
    load_validation_quality_assessment,
)
from app.services.validator_assignment_service import (
    ValidatorProtocolConflictError,
    ValidatorProtocolNotFoundError,
    ValidatorProtocolRelationshipError,
    ValidatorProtocolStorageError,
    _ensure_within,
    _validate_identifier,
    get_validator_protocol_root,
)
from app.utils.protocol_serialization import atomic_create_json, protocol_fingerprint


class ValidatorPerformanceEventError(ValidatorProtocolConflictError):
    pass


def build_validator_performance_event_id(assessment_id: str) -> str:
    return "validator_performance_event_" + protocol_fingerprint(
        {
            "event_version": VALIDATOR_PERFORMANCE_EVENT_VERSION,
            "validation_quality_assessment_id": assessment_id,
        }
    )


def get_validator_performance_event_path(
    protocol_data_root: Path, validator_node_id: str, event_id: str
) -> Path:
    _validate_identifier(validator_node_id, "validator node")
    _validate_identifier(event_id, "validator performance event")
    root = get_validator_protocol_root(protocol_data_root) / "performance-events" / validator_node_id
    path = root / f"{event_id}.json"
    _ensure_within(path, root)
    return path


def create_validator_performance_event(
    protocol_data_root: Path,
    *,
    project_id: str,
    routing_id: str,
    assessment_id: str,
) -> ValidatorPerformanceEvent:
    assessment = load_validation_quality_assessment(
        protocol_data_root, project_id, routing_id, assessment_id
    )
    if assessment is None:
        raise ValidatorProtocolNotFoundError("Validation quality assessment not found")
    if assessment.status != ValidationQualityAssessmentStatus.FINALIZED:
        raise ValidatorPerformanceEventError("Only finalized assessments create performance events")

    event_id = build_validator_performance_event_id(assessment_id)
    source_payload = {
        "event_version": VALIDATOR_PERFORMANCE_EVENT_VERSION,
        "validation_quality_assessment_id": assessment_id,
        "assessment_source_fingerprint": assessment.source_fingerprint,
        "validator_node_id": assessment.validator_node_id,
        "operator_id": assessment.validator_operator_id,
        "project_id": assessment.project_id,
        "routing_id": assessment.routing_id,
        "finding_cluster_id": assessment.finding_cluster_id,
        "category": assessment.category.value,
        "assignment_role": assessment.assignment_role.value,
        "final_validation_consensus_id": assessment.final_validation_consensus_id,
        "component_scores": {
            "validity": assessment.validity_accuracy,
            "reproduction": assessment.reproduction_accuracy,
            "root_cause": assessment.root_cause_accuracy,
            "severity": assessment.severity_accuracy,
            "impact": assessment.impact_accuracy,
            "protocol_compliance": assessment.protocol_compliance,
        },
        "validation_quality_score": assessment.validation_quality_score,
    }
    event = ValidatorPerformanceEvent(
        validator_performance_event_id=event_id,
        validator_node_id=assessment.validator_node_id,
        operator_id=assessment.validator_operator_id,
        project_id=assessment.project_id,
        routing_id=assessment.routing_id,
        finding_cluster_id=assessment.finding_cluster_id,
        category=assessment.category,
        assignment_role=assessment.assignment_role,
        validation_quality_assessment_id=assessment_id,
        final_validation_consensus_id=assessment.final_validation_consensus_id,
        validity_correct=assessment.validity_accuracy == 1,
        reproduction_correct=(
            None if assessment.reproduction_accuracy is None else assessment.reproduction_accuracy == 1
        ),
        root_cause_correct=(
            None if assessment.root_cause_accuracy is None else assessment.root_cause_accuracy == 1
        ),
        severity_correct=(
            None if assessment.severity_accuracy is None else assessment.severity_accuracy == 1
        ),
        impact_correct=(
            None if assessment.impact_accuracy is None else assessment.impact_accuracy == 1
        ),
        protocol_compliant=assessment.protocol_compliance == 1,
        validation_quality_score=assessment.validation_quality_score,
        finalized_at=assessment.finalized_at,
        source_fingerprint=protocol_fingerprint(source_payload),
    )
    path = get_validator_performance_event_path(
        protocol_data_root, event.validator_node_id, event_id
    )
    existing = load_validator_performance_event(
        protocol_data_root, event.validator_node_id, event_id
    )
    if existing is not None:
        if existing != event:
            raise ValidatorPerformanceEventError("Conflicting performance event exists")
        return existing
    if not atomic_create_json(path, event, temporary_prefix=".validator-performance-event-"):
        raced = load_validator_performance_event(
            protocol_data_root, event.validator_node_id, event_id
        )
        if raced != event:
            raise ValidatorPerformanceEventError("Conflicting performance event exists")
        return raced
    return event


def load_validator_performance_event(
    protocol_data_root: Path, validator_node_id: str, event_id: str
) -> ValidatorPerformanceEvent | None:
    path = get_validator_performance_event_path(protocol_data_root, validator_node_id, event_id)
    if not path.exists():
        return None
    try:
        value = ValidatorPerformanceEvent.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
        raise ValidatorProtocolStorageError("Stored validator performance event is malformed") from exc
    if value.validator_node_id != validator_node_id:
        raise ValidatorProtocolRelationshipError("Performance event belongs to another validator")
    return value


def list_validator_performance_events(
    protocol_data_root: Path,
    validator_node_id: str,
    *,
    category: str | None = None,
) -> list[ValidatorPerformanceEvent]:
    _validate_identifier(validator_node_id, "validator node")
    root = get_validator_protocol_root(protocol_data_root) / "performance-events" / validator_node_id
    values = []
    for path in sorted(root.glob("*.json")) if root.exists() else []:
        event = load_validator_performance_event(protocol_data_root, validator_node_id, path.stem)
        if event is not None and (category is None or event.category.value == category):
            values.append(event)
    return sorted(values, key=lambda item: item.validator_performance_event_id)
