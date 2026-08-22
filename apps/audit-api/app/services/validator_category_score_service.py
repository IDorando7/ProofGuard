from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError

from app.schemas.validator_performance import (
    NEUTRAL_VALIDATOR_SCORE,
    VALIDATOR_CATEGORY_SCORE_VERSION,
    ValidatorCategoryScore,
    ValidatorCategoryScorePolicyV1,
    quantize_score,
)
from app.services.validator_assignment_service import (
    ValidatorProtocolNotFoundError,
    ValidatorProtocolRelationshipError,
    ValidatorProtocolStorageError,
    _ensure_within,
    _validate_identifier,
    get_validator_protocol_root,
)
from app.services.validator_category_performance_service import (
    load_validator_category_performance,
)
from app.utils.protocol_serialization import atomic_write_json, protocol_fingerprint


def build_validator_category_score_id(validator_node_id: str, category: str) -> str:
    return "validator_category_score_" + protocol_fingerprint(
        {
            "score_version": VALIDATOR_CATEGORY_SCORE_VERSION,
            "validator_node_id": validator_node_id,
            "category": category,
        }
    )


def get_validator_category_score_path(
    protocol_data_root: Path, validator_node_id: str, category: str
) -> Path:
    _validate_identifier(validator_node_id, "validator node")
    _validate_identifier(category, "category")
    root = get_validator_protocol_root(protocol_data_root) / "category-scores" / validator_node_id
    path = root / f"{category}.json"
    _ensure_within(path, root)
    return path


def metric_rate(correct: int, evaluated: int, neutral: Decimal = NEUTRAL_VALIDATOR_SCORE) -> Decimal:
    if correct < 0 or evaluated < 0 or correct > evaluated:
        raise ValueError("Invalid validator metric counters")
    return neutral if evaluated == 0 else quantize_score(Decimal(correct) / Decimal(evaluated))


def calculate_validator_category_score_values(performance, policy=None):
    policy = policy or ValidatorCategoryScorePolicyV1()
    metrics = {
        "validity_accuracy": metric_rate(performance.validity_correct, performance.validity_evaluated, policy.neutral_default),
        "reproduction_accuracy": metric_rate(performance.reproduction_correct, performance.reproduction_evaluated, policy.neutral_default),
        "root_cause_accuracy": metric_rate(performance.root_cause_correct, performance.root_cause_evaluated, policy.neutral_default),
        "severity_accuracy": metric_rate(performance.severity_correct, performance.severity_evaluated, policy.neutral_default),
        "impact_accuracy": metric_rate(performance.impact_correct, performance.impact_evaluated, policy.neutral_default),
        "protocol_compliance": metric_rate(performance.protocol_compliant_count, performance.protocol_compliance_evaluated, policy.neutral_default),
        "assignment_reliability": metric_rate(performance.completed_assignments, performance.resolved_validations, policy.neutral_default),
    }
    raw_score = quantize_score(
        policy.validity_weight * metrics["validity_accuracy"]
        + policy.reproduction_weight * metrics["reproduction_accuracy"]
        + policy.root_cause_weight * metrics["root_cause_accuracy"]
        + policy.severity_weight * metrics["severity_accuracy"]
        + policy.impact_weight * metrics["impact_accuracy"]
        + policy.protocol_compliance_weight * metrics["protocol_compliance"]
        + policy.assignment_reliability_weight * metrics["assignment_reliability"]
    )
    confidence, final_score = apply_experience_shrinkage(
        raw_score, performance.resolved_validations, policy
    )
    return metrics, raw_score, confidence, final_score


def apply_experience_shrinkage(raw_score: Decimal, resolved_validations: int, policy=None):
    policy = policy or ValidatorCategoryScorePolicyV1()
    if resolved_validations < 0:
        raise ValueError("Resolved validation count cannot be negative")
    confidence = quantize_score(
        min(
            Decimal("1"),
            Decimal(resolved_validations) / Decimal(policy.full_confidence_resolved_validations),
        )
    )
    final_score = quantize_score(
        policy.neutral_default + confidence * (raw_score - policy.neutral_default)
    )
    return confidence, final_score


def calculate_validator_category_score(
    protocol_data_root: Path,
    *,
    validator_node_id: str,
    category: str,
    policy: ValidatorCategoryScorePolicyV1 | None = None,
) -> ValidatorCategoryScore:
    policy = policy or ValidatorCategoryScorePolicyV1()
    performance = load_validator_category_performance(
        protocol_data_root, validator_node_id, category
    )
    if performance is None:
        raise ValidatorProtocolNotFoundError("Validator category performance not found")
    metrics, raw_score, confidence, final_score = calculate_validator_category_score_values(
        performance, policy
    )
    source_payload = {
        "score_version": VALIDATOR_CATEGORY_SCORE_VERSION,
        "score_policy": policy,
        "validator_node_id": validator_node_id,
        "operator_id": performance.operator_id,
        "category": category,
        "performance_id": performance.validator_category_performance_id,
        "performance_source_fingerprint": performance.source_fingerprint,
        "resolved_validations": performance.resolved_validations,
        "metrics": metrics,
        "raw_score": raw_score,
        "experience_confidence": confidence,
        "final_score": final_score,
    }
    existing = load_validator_category_score(protocol_data_root, validator_node_id, category)
    now = datetime.now(timezone.utc)
    score = ValidatorCategoryScore(
        validator_category_score_id=build_validator_category_score_id(validator_node_id, category),
        score_policy=policy,
        validator_node_id=validator_node_id,
        operator_id=performance.operator_id,
        category=category,
        validator_category_performance_id=performance.validator_category_performance_id,
        performance_source_fingerprint=performance.source_fingerprint,
        resolved_validations=performance.resolved_validations,
        **metrics,
        raw_score=raw_score,
        experience_confidence=confidence,
        final_score=final_score,
        source_fingerprint=protocol_fingerprint(source_payload),
        calculated_at=now,
        created_at=existing.created_at if existing else now,
    )
    atomic_write_json(
        get_validator_category_score_path(protocol_data_root, validator_node_id, category),
        score,
        temporary_prefix=".validator-category-score-",
    )
    return score


def load_validator_category_score(
    protocol_data_root: Path, validator_node_id: str, category: str
) -> ValidatorCategoryScore | None:
    path = get_validator_category_score_path(protocol_data_root, validator_node_id, category)
    if not path.exists():
        return None
    try:
        value = ValidatorCategoryScore.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
        raise ValidatorProtocolStorageError("Stored validator category score is malformed") from exc
    if value.validator_node_id != validator_node_id or value.category.value != category:
        raise ValidatorProtocolRelationshipError("Validator category score identity mismatch")
    return value
