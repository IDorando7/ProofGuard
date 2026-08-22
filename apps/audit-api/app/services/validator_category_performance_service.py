from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError

from app.schemas.validator_attestation import ValidatorAssignmentRole
from app.schemas.validator_performance import (
    VALIDATOR_CATEGORY_PERFORMANCE_VERSION,
    ValidatorCategoryScorePolicyV1,
    ValidatorCategoryPerformance,
    quantize_score,
)
from app.services.node_registry_service import load_node
from app.services.validator_assignment_service import (
    ValidatorProtocolConflictError,
    ValidatorProtocolNotFoundError,
    ValidatorProtocolRelationshipError,
    ValidatorProtocolStorageError,
    _ensure_within,
    _validate_identifier,
    get_validator_protocol_root,
)
from app.services.validator_performance_event_service import (
    create_validator_performance_event,
    list_validator_performance_events,
)
from app.utils.protocol_serialization import atomic_write_json, protocol_fingerprint


class ValidatorCategoryPerformanceError(ValidatorProtocolConflictError):
    pass


def build_validator_category_performance_id(validator_node_id: str, category: str) -> str:
    return "validator_category_performance_" + protocol_fingerprint(
        {
            "performance_version": VALIDATOR_CATEGORY_PERFORMANCE_VERSION,
            "validator_node_id": validator_node_id,
            "category": category,
        }
    )


def get_validator_category_performance_path(
    protocol_data_root: Path, validator_node_id: str, category: str
) -> Path:
    _validate_identifier(validator_node_id, "validator node")
    _validate_identifier(category, "category")
    root = get_validator_protocol_root(protocol_data_root) / "category-performance" / validator_node_id
    path = root / f"{category}.json"
    _ensure_within(path, root)
    return path


def rebuild_validator_category_performance(
    protocol_data_root: Path,
    *,
    validator_node_id: str,
    category: str,
) -> ValidatorCategoryPerformance:
    node = load_node(protocol_data_root, validator_node_id)
    if node is None:
        raise ValidatorProtocolNotFoundError("Validator node not found")
    events = list_validator_performance_events(
        protocol_data_root, validator_node_id, category=category
    )
    if not events:
        raise ValidatorProtocolNotFoundError("No resolved validator performance events found")
    if any(event.operator_id != node.operator_id for event in events):
        raise ValidatorCategoryPerformanceError("Validator operator identity changed across events")

    for event in events:
        current = create_validator_performance_event(
            protocol_data_root,
            project_id=event.project_id,
            routing_id=event.routing_id,
            assessment_id=event.validation_quality_assessment_id,
        )
        if current != event:
            raise ValidatorCategoryPerformanceError("Performance event no longer matches assessment")

    def count_bool(field: str) -> tuple[int, int]:
        values = [getattr(item, field) for item in events]
        evaluated = [value for value in values if value is not None]
        return len(evaluated), sum(value is True for value in evaluated)

    validity_evaluated, validity_correct = count_bool("validity_correct")
    reproduction_evaluated, reproduction_correct = count_bool("reproduction_correct")
    root_evaluated, root_correct = count_bool("root_cause_correct")
    severity_evaluated, severity_correct = count_bool("severity_correct")
    impact_evaluated, impact_correct = count_bool("impact_correct")
    compliance_evaluated, compliance_correct = count_bool("protocol_compliant")
    score_sum = sum((item.validation_quality_score for item in events), Decimal("0")).quantize(Decimal("0.000001"))
    source_payload = {
        "performance_version": VALIDATOR_CATEGORY_PERFORMANCE_VERSION,
        "validator_node_id": validator_node_id,
        "operator_id": node.operator_id,
        "category": category,
        "score_policy_version": ValidatorCategoryScorePolicyV1().policy_version,
        "events": [
            {"id": item.validator_performance_event_id, "source_fingerprint": item.source_fingerprint}
            for item in events
        ],
    }
    existing = load_validator_category_performance(
        protocol_data_root, validator_node_id, category
    )
    now = datetime.now(timezone.utc)
    performance = ValidatorCategoryPerformance(
        validator_category_performance_id=build_validator_category_performance_id(
            validator_node_id, category
        ),
        validator_node_id=validator_node_id,
        operator_id=node.operator_id,
        category=category,
        resolved_validations=len(events),
        authoritative_resolved_validations=sum(
            item.assignment_role == ValidatorAssignmentRole.AUTHORITATIVE for item in events
        ),
        shadow_resolved_validations=sum(
            item.assignment_role == ValidatorAssignmentRole.SHADOW for item in events
        ),
        validity_evaluated=validity_evaluated,
        validity_correct=validity_correct,
        reproduction_evaluated=reproduction_evaluated,
        reproduction_correct=reproduction_correct,
        root_cause_evaluated=root_evaluated,
        root_cause_correct=root_correct,
        severity_evaluated=severity_evaluated,
        severity_correct=severity_correct,
        impact_evaluated=impact_evaluated,
        impact_correct=impact_correct,
        protocol_compliance_evaluated=compliance_evaluated,
        protocol_compliant_count=compliance_correct,
        protocol_violation_count=compliance_evaluated - compliance_correct,
        completed_assignments=len(events),
        quality_score_sum=score_sum,
        quality_score_count=len(events),
        average_quality_score=quantize_score(score_sum / len(events)),
        source_event_ids=[item.validator_performance_event_id for item in events],
        source_fingerprint=protocol_fingerprint(source_payload),
        first_resolved_validation_at=min(item.finalized_at for item in events),
        last_resolved_validation_at=max(item.finalized_at for item in events),
        rebuilt_at=now,
        created_at=existing.created_at if existing else now,
    )
    atomic_write_json(
        get_validator_category_performance_path(protocol_data_root, validator_node_id, category),
        performance,
        temporary_prefix=".validator-category-performance-",
    )
    return performance


def load_validator_category_performance(
    protocol_data_root: Path, validator_node_id: str, category: str
) -> ValidatorCategoryPerformance | None:
    path = get_validator_category_performance_path(protocol_data_root, validator_node_id, category)
    if not path.exists():
        return None
    try:
        value = ValidatorCategoryPerformance.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
        raise ValidatorProtocolStorageError("Stored validator category performance is malformed") from exc
    if value.validator_node_id != validator_node_id or value.category.value != category:
        raise ValidatorProtocolRelationshipError("Validator category performance identity mismatch")
    return value
