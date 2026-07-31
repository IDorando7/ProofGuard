from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas.contribution import (
    ContributionCalculationRequest,
    ContributionComponentName,
    ContributionComponents,
    ContributionEligibilityStatus,
    ContributionScoreRecord,
    ContributionSignal,
)


def _record(**updates):
    now = datetime.now(timezone.utc)
    values = {
        "score_id": "score-1",
        "scoring_version": "contribution_v0",
        "submission_id": "submission-1",
        "project_id": "project-1",
        "finding_id": "finding-1",
        "node_id": "node-1",
        "validation_id": "validation-1",
        "reproduction_id": "reproduction-1",
        "total_score": 90,
        "eligibility_status": "eligible",
        "eligible_for_reward": True,
        "eligibility_reasons": ["All hard requirements are satisfied."],
        "components": ContributionComponents(raw_total=90),
        "signals": [],
        "reason": "Eligible deterministic contribution.",
        "created_at": now,
        "updated_at": now,
    }
    values.update(updates)
    return ContributionScoreRecord(**values)


def test_required_enums_have_exact_values():
    assert {item.value for item in ContributionEligibilityStatus} == {"eligible", "ineligible", "pending"}
    assert {item.value for item in ContributionComponentName} == {
        "validity",
        "severity",
        "reproducibility",
        "uniqueness",
        "quality",
        "penalties",
    }


def test_signal_validates_required_text_and_point_range():
    signal = ContributionSignal(
        code="VALIDATION_ACCEPTED",
        component="validity",
        points=30,
        message="Validation accepted the finding.",
        source="validation",
    )
    assert signal.points == 30
    for values in (
        {"code": "", "points": 0, "message": "message"},
        {"code": "code", "points": 0, "message": ""},
        {"code": "code", "points": -101, "message": "message"},
        {"code": "code", "points": 101, "message": "message"},
    ):
        with pytest.raises(ValidationError):
            ContributionSignal(component="validity", **values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("validity", 31),
        ("severity", 26),
        ("reproducibility", 26),
        ("uniqueness", 16),
        ("quality", 11),
        ("penalties", 1),
        ("penalties", -101),
        ("raw_total", 106),
    ],
)
def test_component_ranges_are_enforced(field, value):
    with pytest.raises(ValidationError):
        ContributionComponents(**{field: value})


@pytest.mark.parametrize("score", [-1, 101])
def test_total_score_range_is_enforced(score):
    with pytest.raises(ValidationError):
        _record(total_score=score)


@pytest.mark.parametrize("status", ["pending", "ineligible"])
def test_noneligible_status_cannot_be_reward_eligible(status):
    with pytest.raises(ValidationError):
        _record(eligibility_status=status, eligible_for_reward=True)


def test_eligible_status_may_remain_false_until_reward_policy_uses_it():
    assert _record(eligibility_status="eligible", eligible_for_reward=False).eligible_for_reward is False


def test_record_requires_reasons_reason_and_aware_timestamps():
    with pytest.raises(ValidationError):
        _record(eligibility_reasons=[])
    with pytest.raises(ValidationError):
        _record(reason=" ")
    with pytest.raises(ValidationError):
        _record(created_at=datetime.now())


def test_record_contains_no_reward_token_or_private_key_fields():
    fields = set(ContributionScoreRecord.model_fields)
    assert not fields.intersection({"reward_amount", "token_amount", "private_key", "mnemonic"})


@pytest.mark.parametrize("field", ["total_score", "penalties", "eligibility_status", "private_key"])
def test_calculation_request_rejects_client_controlled_fields(field):
    with pytest.raises(ValidationError):
        ContributionCalculationRequest.model_validate({field: 100})
