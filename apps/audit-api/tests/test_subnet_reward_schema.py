from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.subnet_reward import (
    CategoryPoolAllocation,
    SubnetRewardCycle,
    SubnetRewardCycleCreateRequest,
    SubnetRewardCycleStatus,
    SubnetRewardEligibilityStatus,
    SubnetRewardEventStatus,
    SubnetRewardExclusion,
    SubnetRewardExclusionReason,
    SubnetRewardProcessingStatus,
    SubnetRewardSourceSnapshot,
    SubnetSubmissionRewardAllocation,
)


NOW = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)
FP = "a" * 64


def _snapshot():
    return SubnetRewardSourceSnapshot(
        routing_id="routing-1",
        routing_source_fingerprint=FP,
        routing_assignment_id="assignment-1",
        routing_assignment_membership_status="probation",
        routing_assignment_category_score=Decimal("0.55"),
        submission_id="submission-1",
        submission_source_fingerprint=FP,
        contribution_score_id="contribution-1",
        contribution_source_fingerprint=FP,
        contribution_score=Decimal("70"),
        validation_decision_id="validation-1",
        validation_source_fingerprint=FP,
        reproduction_result_id="reproduction-1",
        reproduction_source_fingerprint=FP,
        existing_reward_event_ids=[],
    )


def _allocation():
    return SubnetSubmissionRewardAllocation(
        allocation_id="allocation-1",
        reward_cycle_id="cycle-1",
        project_id="project-1",
        routing_id="routing-1",
        routing_assignment_id="assignment-1",
        submission_id="submission-1",
        finding_id="finding-1",
        node_id="node-1",
        subnet_id="subnet_access_control",
        category="access_control",
        selection_type="exploration",
        assignment_mode="shadow",
        membership_status="probation",
        contribution_score=Decimal("70"),
        category_score=Decimal("0.55"),
        category_multiplier=Decimal("0.95"),
        membership_multiplier=Decimal("0.90"),
        raw_weight=Decimal("59.850000"),
        normalized_share=Decimal("1"),
        reward_points=Decimal("100.000000"),
        eligibility_status="eligible",
        eligibility_reasons=["Eligible routed contribution."],
        source_snapshot=_snapshot(),
        source_fingerprint=FP,
        created_at=NOW,
    )


def _pool(allocation=True):
    allocations = [_allocation()] if allocation else []
    return CategoryPoolAllocation(
        category="access_control",
        subnet_id="subnet_access_control",
        requested_weight=Decimal("1"),
        normalized_weight=Decimal("1"),
        allocated_pool_points=Decimal("100.000000"),
        eligible_submissions=len(allocations),
        ineligible_submissions=0,
        total_raw_weight=Decimal("59.850000") if allocations else Decimal("0"),
        distributed_points=(
            Decimal("100.000000") if allocations else Decimal("0.000000")
        ),
        undistributed_points=(
            Decimal("0.000000") if allocations else Decimal("100.000000")
        ),
        allocations=allocations,
        exclusions=[],
        warnings=[],
    )


def test_required_enums_are_complete():
    assert {item.value for item in SubnetRewardCycleStatus} == {
        "draft",
        "calculated",
        "finalized",
    }
    assert {item.value for item in SubnetRewardProcessingStatus} == {
        "created",
        "calculated",
        "unchanged",
        "finalized",
        "already_finalized",
    }
    assert {item.value for item in SubnetRewardEligibilityStatus} == {
        "eligible",
        "ineligible",
    }
    assert SubnetRewardEventStatus.APPLIED.value == "applied"
    assert {
        "candidate_shadow_ineligible",
        "already_rewarded",
        "unsafe_submission",
        "conflicting_reward_event",
    } <= {item.value for item in SubnetRewardExclusionReason}


def test_create_request_uses_decimal_and_rejects_invalid_public_inputs():
    request = SubnetRewardCycleCreateRequest(
        routing_id="routing-1",
        total_pool_points="10000.000000",
        category_weights={"access-control": "3", "reentrancy": "2"},
    )
    assert request.total_pool_points == Decimal("10000.000000")
    assert set(item.value for item in request.category_weights) == {
        "access_control",
        "reentrancy",
    }
    for value in ("0", "-1", "1000000000.000001"):
        with pytest.raises(ValidationError):
            SubnetRewardCycleCreateRequest(
                routing_id="routing-1",
                total_pool_points=value,
            )
    with pytest.raises(ValidationError):
        SubnetRewardCycleCreateRequest(
            routing_id="routing-1",
            total_pool_points=100.0,
        )
    with pytest.raises(ValidationError):
        SubnetRewardCycleCreateRequest(
            routing_id="routing-1",
            total_pool_points="100",
            node_ids=["node-1"],
        )


def test_category_weights_must_be_positive_and_supported():
    with pytest.raises(ValidationError):
        SubnetRewardCycleCreateRequest(
            routing_id="routing-1",
            total_pool_points="100",
            category_weights={"access_control": "0"},
        )
    with pytest.raises(ValidationError):
        SubnetRewardCycleCreateRequest(
            routing_id="routing-1",
            total_pool_points="100",
            category_weights={"not-a-category": "1"},
        )


def test_exclusion_requires_reasons_and_valid_fingerprints():
    exclusion = SubnetRewardExclusion(
        submission_id="submission-1",
        assignment_id="assignment-1",
        node_id="node-1",
        category="access_control",
        reasons=["finding_duplicate"],
        explanation=["Validation marked the finding duplicate."],
    )
    assert exclusion.reasons == [SubnetRewardExclusionReason.FINDING_DUPLICATE]
    with pytest.raises(ValidationError):
        exclusion.model_copy(update={"reasons": []}).__class__.model_validate(
            {**exclusion.model_dump(), "reasons": []}
        )
    with pytest.raises(ValidationError):
        SubnetRewardSourceSnapshot.model_validate(
            {**_snapshot().model_dump(), "routing_source_fingerprint": "bad"}
        )


def test_allocation_formula_context_and_pool_conservation_validate():
    allocation = _allocation()
    assert allocation.raw_weight == Decimal("59.850000")
    with pytest.raises(ValidationError):
        SubnetSubmissionRewardAllocation.model_validate(
            {**allocation.model_dump(), "raw_weight": Decimal("1")}
        )
    with pytest.raises(ValidationError):
        CategoryPoolAllocation.model_validate(
            {**_pool().model_dump(), "undistributed_points": Decimal("1")}
        )


def test_cycle_lifecycle_and_conservation():
    draft_pool = _pool(allocation=False)
    draft = SubnetRewardCycle(
        reward_cycle_id="cycle-1",
        project_id="project-1",
        routing_id="routing-1",
        routing_source_fingerprint=FP,
        status="draft",
        total_pool_points=Decimal("100.000000"),
        total_distributed_points=Decimal("0.000000"),
        total_undistributed_points=Decimal("100.000000"),
        category_weights={"access_control": Decimal("1")},
        category_pools=[draft_pool],
        node_summaries=[],
        request_fingerprint=FP,
        source_fingerprint=None,
        created_at=NOW,
        updated_at=NOW,
    )
    assert draft.status == SubnetRewardCycleStatus.DRAFT
    with pytest.raises(ValidationError):
        SubnetRewardCycle.model_validate(
            {
                **draft.model_dump(),
                "status": "calculated",
                "source_fingerprint": None,
                "calculated_at": NOW,
            }
        )
    with pytest.raises(ValidationError):
        SubnetRewardCycle.model_validate(
            {
                **draft.model_dump(),
                "total_undistributed_points": Decimal("99.000000"),
            }
        )


def test_reward_models_contain_no_payment_or_execution_fields():
    fields = (
        set(SubnetRewardCycle.model_fields)
        | set(SubnetSubmissionRewardAllocation.model_fields)
    )
    assert {
        "wallet",
        "token",
        "currency",
        "stake",
        "poc",
        "payment",
    }.isdisjoint(fields)
