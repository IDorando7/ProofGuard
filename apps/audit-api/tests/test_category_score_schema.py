from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas.category_score import (
    CategoryScoreBand,
    CategoryScoreComponents,
    CategoryScorePenalties,
    CategoryScoreProcessingStatus,
    CategoryScoreRebuildRequest,
    CategoryScoreRecord,
    CategoryScoreWeightedComponents,
    CategoryScoreWeights,
)


NOW = datetime.now(timezone.utc)
FINGERPRINT = "a" * 64


def _record(**updates):
    values = {
        "score_id": "category_score_node-1_access_control",
        "scoring_version": "category_score_v0",
        "node_id": "node-1",
        "category": "access_control",
        "performance_id": "performance_node-1_access_control",
        "performance_source_fingerprint": FINGERPRINT,
        "components": CategoryScoreComponents(
            precision=0.5,
            reproduction_rate=0.5,
            uniqueness_rate=0.5,
            contribution_quality=0.5,
            consistency=0.5,
            experience_confidence=0.5,
        ),
        "weights": CategoryScoreWeights(),
        "weighted_components": CategoryScoreWeightedComponents(
            precision_contribution=0.15,
            reproduction_contribution=0.10,
            uniqueness_contribution=0.075,
            contribution_quality_contribution=0.10,
            consistency_contribution=0.05,
            experience_contribution=0.025,
            positive_total=0.50,
        ),
        "penalties": CategoryScorePenalties(
            duplicate_penalty=0,
            out_of_scope_penalty=0,
            insufficient_evidence_penalty=0,
            rejected_penalty=0,
            unsafe_penalty=0,
            unsupported_penalty=0,
            total_penalty=0,
        ),
        "raw_score_before_penalties": 0.5,
        "raw_score_after_penalties": 0.5,
        "neutral_baseline": 0.5,
        "confidence_adjusted_score": 0.5,
        "category_score": 0.5,
        "score_band": "developing",
        "finalized_submissions": 5,
        "accepted_unique_submissions": 2,
        "source_fingerprint": FINGERPRINT,
        "explanation": ["Deterministic score explanation."],
        "calculated_at": NOW,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(updates)
    return CategoryScoreRecord(**values)


def test_enums_contain_all_required_values():
    assert {item.value for item in CategoryScoreBand} == {
        "insufficient_data",
        "weak",
        "developing",
        "strong",
        "expert",
    }
    assert {item.value for item in CategoryScoreProcessingStatus} == {
        "calculated",
        "unchanged",
    }


@pytest.mark.parametrize(
    "field",
    [
        "precision",
        "reproduction_rate",
        "uniqueness_rate",
        "contribution_quality",
        "consistency",
        "experience_confidence",
    ],
)
@pytest.mark.parametrize("value", [-0.001, 1.001])
def test_components_reject_values_outside_unit_interval(field, value):
    values = {
        "precision": 0.5,
        "reproduction_rate": 0.5,
        "uniqueness_rate": 0.5,
        "contribution_quality": 0.5,
        "consistency": 0.5,
        "experience_confidence": 0.5,
    }
    values[field] = value
    with pytest.raises(ValidationError):
        CategoryScoreComponents(**values)


def test_weights_are_fixed_defaults_and_must_sum_to_one():
    weights = CategoryScoreWeights()
    assert sum(weights.model_dump().values()) == pytest.approx(1)
    with pytest.raises(ValidationError):
        CategoryScoreWeights(precision_weight=0.29)


def test_weighted_component_total_must_match_contributions():
    with pytest.raises(ValidationError):
        CategoryScoreWeightedComponents(
            precision_contribution=0.1,
            reproduction_contribution=0.1,
            uniqueness_contribution=0.1,
            contribution_quality_contribution=0.1,
            consistency_contribution=0.1,
            experience_contribution=0.1,
            positive_total=0.9,
        )


def test_penalty_total_is_capped_and_must_match_components():
    with pytest.raises(ValidationError):
        CategoryScorePenalties(
            duplicate_penalty=0.1,
            out_of_scope_penalty=0.15,
            insufficient_evidence_penalty=0.1,
            rejected_penalty=0.12,
            unsafe_penalty=0.3,
            unsupported_penalty=0,
            total_penalty=0.61,
        )
    valid = CategoryScorePenalties(
        duplicate_penalty=0.1,
        out_of_scope_penalty=0.15,
        insufficient_evidence_penalty=0.1,
        rejected_penalty=0.12,
        unsafe_penalty=0.3,
        unsupported_penalty=0,
        total_penalty=0.6,
    )
    assert valid.total_penalty == 0.6


def test_valid_score_record_succeeds_and_requires_identity_fields():
    assert _record().scoring_version == "category_score_v0"
    for field in ("performance_id", "scoring_version"):
        values = _record().model_dump()
        values.pop(field)
        with pytest.raises(ValidationError):
            CategoryScoreRecord(**values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("category_score", -0.01),
        ("category_score", 1.01),
        ("raw_score_before_penalties", -0.01),
        ("raw_score_after_penalties", 1.01),
        ("source_fingerprint", "ABC"),
        ("performance_source_fingerprint", "f" * 63),
        ("explanation", []),
        ("explanation", [""]),
    ],
)
def test_score_record_rejects_invalid_scores_fingerprints_and_explanations(
    field,
    value,
):
    with pytest.raises(ValidationError):
        _record(**{field: value})


def test_score_record_has_no_membership_ranking_routing_or_financial_fields():
    fields = set(CategoryScoreRecord.model_fields)
    forbidden = {
        "membership_status",
        "subnet_member_status",
        "rank",
        "routing_assignment",
        "token",
        "stake",
        "wallet",
        "reward_pool",
    }
    assert not fields & forbidden


@pytest.mark.parametrize(
    "payload",
    [
        {"weights": {"precision_weight": 1}},
        {"category_score": 1},
        {"score_band": "expert"},
        {"penalties": {"unsafe_penalty": 0}},
    ],
)
def test_public_rebuild_request_rejects_client_controlled_values(payload):
    with pytest.raises(ValidationError):
        CategoryScoreRebuildRequest.model_validate(payload)
