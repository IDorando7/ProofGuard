from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas.reward import (
    PenaltyEvent,
    RewardAllocation,
    RewardCycle,
    RewardCycleCreate,
    RewardCycleStatus,
    RewardEligibilityReasonCode,
    RewardEvent,
    RewardEventStatus,
    RewardUnit,
    RewardWeightComponents,
)


NOW = datetime.now(timezone.utc)
FINGERPRINT = "a" * 64


def _weight(**updates):
    values = {
        "contribution_score": 90,
        "contribution_weight": 90,
        "reputation_score": 0.8,
        "reputation_multiplier": 1.15,
        "category_multiplier": 1,
        "raw_weight": 103.5,
    }
    values.update(updates)
    return RewardWeightComponents(**values)


def _allocation(**updates):
    values = {
        "submission_id": "submission-1",
        "node_id": "node-1",
        "project_id": "project-1",
        "finding_id": "finding-1",
        "category": "access_control",
        "eligible": True,
        "eligibility_reason_code": "eligible",
        "eligibility_reasons": ["Eligible."],
        "weight_components": _weight(),
        "allocation_ratio": 1,
        "reward_amount": 1000,
        "reward_unit": "protocol_points",
    }
    values.update(updates)
    return RewardAllocation(**values)


def _cycle(**updates):
    values = {
        "cycle_id": "cycle-1",
        "reward_version": "reward_v0",
        "project_id": "project-1",
        "reward_pool": 1000,
        "reward_unit": "protocol_points",
        "status": "calculated",
        "submission_ids": ["submission-1"],
        "allocations": [_allocation()],
        "eligible_submissions": 1,
        "ineligible_submissions": 0,
        "total_raw_weight": 103.5,
        "total_allocated": 1000,
        "undistributed_amount": 0,
        "source_fingerprint": FINGERPRINT,
        "created_at": NOW,
        "calculated_at": NOW,
        "updated_at": NOW,
    }
    values.update(updates)
    return RewardCycle(**values)


def test_reward_enums_have_exact_values():
    assert [item.value for item in RewardUnit] == ["protocol_points"]
    assert [item.value for item in RewardCycleStatus] == ["draft", "calculated", "finalized"]
    assert [item.value for item in RewardEventStatus] == ["calculated", "finalized"]
    assert {item.value for item in RewardEligibilityReasonCode} == {
        "eligible", "contribution_missing", "contribution_pending", "contribution_ineligible",
        "validation_not_accepted", "reproduction_not_confirmed", "duplicate", "out_of_scope",
        "rejected", "unsafe", "unsupported", "already_rewarded", "already_penalized", "source_mismatch",
    }


@pytest.mark.parametrize(
    "updates",
    [
        {"raw_weight": -1},
        {"reputation_score": -0.01},
        {"reputation_score": 1.01},
        {"raw_weight": 99},
    ],
)
def test_weight_components_reject_invalid_values_and_formula(updates):
    with pytest.raises(ValidationError):
        _weight(**updates)


@pytest.mark.parametrize(
    "updates",
    [
        {"allocation_ratio": -0.1},
        {"allocation_ratio": 1.1},
        {"reward_amount": -1},
        {"eligible": False, "eligibility_reason_code": "duplicate", "allocation_ratio": 0.1},
        {"eligible": False, "eligibility_reason_code": "duplicate", "reward_amount": 1},
        {"weight_components": None},
    ],
)
def test_allocation_invariants(updates):
    with pytest.raises(ValidationError):
        _allocation(**updates)


def test_cycle_create_rejects_invalid_pool_duplicates_and_controlled_fields():
    with pytest.raises(ValidationError):
        RewardCycleCreate(project_id="project-1", reward_pool=0)
    with pytest.raises(ValidationError):
        RewardCycleCreate(project_id="project-1", reward_pool=1_000_001)
    with pytest.raises(ValidationError):
        RewardCycleCreate(project_id="project-1", submission_ids=["one", "one"])
    with pytest.raises(ValidationError):
        RewardCycleCreate.model_validate({"project_id": "project-1", "reward_amount": 5})


def test_cycle_requires_valid_fingerprint_timestamps_and_totals():
    with pytest.raises(ValidationError):
        _cycle(source_fingerprint="bad")
    with pytest.raises(ValidationError):
        _cycle(status="finalized", finalized_at=None)
    with pytest.raises(ValidationError):
        _cycle(calculated_at=None)
    with pytest.raises(ValidationError):
        _cycle(total_allocated=1000.01)


def test_reward_and_penalty_models_expose_no_financial_or_wallet_fields():
    assert not ({"wallet", "wallet_address", "token_address", "transaction_hash", "private_key"} & RewardEvent.model_fields.keys())
    assert not ({"wallet", "wallet_address", "token_address", "transaction_hash", "private_key"} & PenaltyEvent.model_fields.keys())


def test_penalty_enforces_no_onchain_execution_or_stake_loss():
    values = {
        "penalty_event_id": "penalty-1",
        "penalty_version": "penalty_v0",
        "status": "recorded",
        "node_id": "node-1",
        "submission_id": "submission-1",
        "project_id": "project-1",
        "finding_id": "finding-1",
        "category": "access_control",
        "reason_code": "unsafe_submission",
        "reward_denied": True,
        "protocol_penalty_points": 25,
        "simulated_stake_loss": 0,
        "executed_onchain": False,
        "requires_human_review": True,
        "source_fingerprint": FINGERPRINT,
        "reason": "Unsafe submission.",
        "created_at": NOW,
        "updated_at": NOW,
    }
    PenaltyEvent(**values)
    with pytest.raises(ValidationError):
        PenaltyEvent(**{**values, "executed_onchain": True})
    with pytest.raises(ValidationError):
        PenaltyEvent(**{**values, "simulated_stake_loss": 1})
