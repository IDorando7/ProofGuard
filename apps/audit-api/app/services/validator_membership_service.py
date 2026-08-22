from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from app.schemas.node import NodeStatus, NodeType
from app.schemas.validator_performance import (
    VALIDATOR_MEMBERSHIP_VERSION,
    ValidatorMembership,
    ValidatorMembershipPolicyV1,
    ValidatorMembershipStatus,
)
from app.services.node_registry_service import load_node
from app.services.validator_assignment_service import (
    ValidatorProtocolNotFoundError,
    ValidatorProtocolRelationshipError,
    ValidatorProtocolStorageError,
    _ensure_within,
    _validate_identifier,
    get_validator_protocol_root,
)
from app.services.validator_category_performance_service import load_validator_category_performance
from app.services.validator_category_score_service import load_validator_category_score
from app.utils.protocol_serialization import atomic_write_json, protocol_fingerprint


def build_validator_membership_id(validator_node_id: str, category: str) -> str:
    return "validator_membership_" + protocol_fingerprint(
        {
            "membership_version": VALIDATOR_MEMBERSHIP_VERSION,
            "validator_node_id": validator_node_id,
            "category": category,
        }
    )


def get_validator_membership_path(
    protocol_data_root: Path, validator_node_id: str, category: str
) -> Path:
    _validate_identifier(validator_node_id, "validator node")
    _validate_identifier(category, "category")
    root = get_validator_protocol_root(protocol_data_root) / "memberships" / validator_node_id
    path = root / f"{category}.json"
    _ensure_within(path, root)
    return path


def derive_validator_membership(
    protocol_data_root: Path,
    *,
    validator_node_id: str,
    category: str,
    policy: ValidatorMembershipPolicyV1 | None = None,
) -> ValidatorMembership:
    policy = policy or ValidatorMembershipPolicyV1()
    node = load_node(protocol_data_root, validator_node_id)
    score = load_validator_category_score(protocol_data_root, validator_node_id, category)
    performance = load_validator_category_performance(protocol_data_root, validator_node_id, category)
    if node is None or score is None or performance is None:
        raise ValidatorProtocolNotFoundError("Validator membership source state not found")
    existing = load_validator_membership(protocol_data_root, validator_node_id, category)
    previous = existing.status if existing else None
    reasons: list[str]
    if node.node_type not in {NodeType.VALIDATOR, NodeType.HYBRID} or category not in node.supported_categories:
        status, reasons = ValidatorMembershipStatus.REMOVED, ["validator_capability_unavailable"]
    elif node.status != NodeStatus.ACTIVE:
        status, reasons = ValidatorMembershipStatus.SUSPENDED, [f"node_{node.status.value}"]
    elif performance.protocol_violation_count > 0:
        status, reasons = ValidatorMembershipStatus.SUSPENDED, [
            "adjudicated_validator_protocol_violation"
        ]
    else:
        status, reasons = _performance_membership(score, performance, previous, policy)

    if (
        existing
        and existing.membership_policy == policy
        and existing.operator_id == node.operator_id
        and existing.score_source_fingerprint == score.source_fingerprint
        and existing.status == status
        and existing.reason_codes == sorted(reasons)
    ):
        return existing

    source_payload = {
        "membership_version": VALIDATOR_MEMBERSHIP_VERSION,
        "membership_policy": policy,
        "validator_node_id": validator_node_id,
        "operator_id": node.operator_id,
        "node_type": node.node_type.value,
        "node_status": node.status.value,
        "supported_category": category in node.supported_categories,
        "category": category,
        "score_id": score.validator_category_score_id,
        "score_source_fingerprint": score.source_fingerprint,
        "performance_source_fingerprint": performance.source_fingerprint,
        "previous_status": previous.value if previous else None,
        "status": status.value,
        "reason_codes": reasons,
    }
    fingerprint = protocol_fingerprint(source_payload)
    if existing and existing.source_fingerprint == fingerprint:
        return existing
    now = datetime.now(timezone.utc)
    membership = ValidatorMembership(
        validator_membership_id=build_validator_membership_id(validator_node_id, category),
        membership_policy=policy,
        validator_node_id=validator_node_id,
        operator_id=node.operator_id,
        category=category,
        validator_category_score_id=score.validator_category_score_id,
        score_source_fingerprint=score.source_fingerprint,
        previous_status=previous,
        status=status,
        reason_codes=reasons,
        source_fingerprint=fingerprint,
        created_at=existing.created_at if existing else now,
        evaluated_at=now,
    )
    atomic_write_json(
        get_validator_membership_path(protocol_data_root, validator_node_id, category),
        membership,
        temporary_prefix=".validator-membership-",
    )
    return membership


def _performance_membership(score, performance, previous, policy):
    no_violations = performance.protocol_violation_count == 0
    reproduction_ready = (
        performance.reproduction_evaluated < policy.expert_minimum_reproduction_samples
        or score.reproduction_accuracy >= policy.expert_minimum_reproduction_accuracy
    )
    if (
        performance.resolved_validations >= policy.expert_minimum_resolved
        and score.validity_accuracy >= policy.expert_minimum_validity_accuracy
        and reproduction_ready
        and no_violations
        and (
            score.final_score >= policy.expert_minimum_score
            or previous == ValidatorMembershipStatus.EXPERT
            and score.final_score >= policy.expert_retention_score
        )
    ):
        return ValidatorMembershipStatus.EXPERT, ["expert_requirements_met"]
    active_threshold = (
        policy.active_retention_score
        if previous in {ValidatorMembershipStatus.ACTIVE, ValidatorMembershipStatus.EXPERT}
        else policy.active_minimum_score
    )
    if (
        performance.resolved_validations >= policy.active_minimum_resolved
        and performance.validity_evaluated >= policy.active_minimum_validity_samples
        and score.final_score >= active_threshold
        and no_violations
    ):
        return ValidatorMembershipStatus.ACTIVE, ["active_requirements_met"]
    if (
        performance.resolved_validations >= policy.probation_minimum_resolved
        and score.final_score >= policy.probation_minimum_score
    ):
        return ValidatorMembershipStatus.PROBATION, ["probation_requirements_met"]
    return ValidatorMembershipStatus.CANDIDATE, ["building_resolved_history"]


def load_validator_membership(
    protocol_data_root: Path, validator_node_id: str, category: str
) -> ValidatorMembership | None:
    path = get_validator_membership_path(protocol_data_root, validator_node_id, category)
    if not path.exists():
        return None
    try:
        value = ValidatorMembership.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
        raise ValidatorProtocolStorageError("Stored validator membership is malformed") from exc
    if value.validator_node_id != validator_node_id or value.category.value != category:
        raise ValidatorProtocolRelationshipError("Validator membership identity mismatch")
    return value
