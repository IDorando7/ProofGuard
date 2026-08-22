from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.task_finding_reward import (
    FindingClusterAllocationReason,
    FindingClusterRewardAllocation,
    FindingParentPoolType,
)
from app.schemas.task_operator_reward import (
    ChiefFinderConfig,
    DuplicateRewardConfig,
    EligibleReportRewardInput,
    OperatorRewardExclusionReason,
    TaskOperatorRewardConfig,
)
from app.services.task_operator_reward_calculator import (
    OperatorRewardCalculationError,
    calculate_cluster_operator_payout,
    quality_weight,
    select_operator_representatives,
    split_chief_and_quality_pools,
)


BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _day4(points="1000.000000"):
    return FindingClusterRewardAllocation(
        finding_cluster_id="cluster-1",
        project_id="project-1",
        routing_id="routing-1",
        category="access_control",
        cluster_source_fingerprint="a" * 64,
        final_severity="High",
        severity_weight="8.000000",
        distinct_operator_count=1,
        uniqueness="1.000000",
        finding_score="8.000000",
        allocation_reason=FindingClusterAllocationReason.POSITIVE_FINDING_SCORE,
        parent_pool_type=FindingParentPoolType.MINER_POOL,
        parent_pool_id="budget-1",
        parent_pool_points=points,
        cluster_reward_points=points,
        source_fingerprint="b" * 64,
    )


def _report(
    index,
    quality,
    *,
    operator=None,
    node=None,
    submitted_at=None,
    chief=True,
):
    operator = operator or f"operator-{index:03d}"
    node = node or f"node-{index:03d}"
    return EligibleReportRewardInput(
        finding_cluster_id="cluster-1",
        submission_id=f"submission-{index:03d}",
        finding_id=f"finding-{index:03d}",
        node_id=node,
        operator_id=operator,
        routing_assignment_id=f"assignment-{index:03d}",
        submitted_at=submitted_at or BASE_TIME + timedelta(minutes=index),
        member_relation="canonical" if index == 1 else "independent_duplicate",
        member_source_fingerprint=f"{index:064x}",
        assessment_id=f"assessment-{index:03d}",
        assessment_source_fingerprint=f"{1000 + index:064x}",
        quality_score=quality,
        chief_root_cause_qualified=chief,
        chief_severity_qualified=chief,
        chief_impact_qualified=chief,
        chief_evidence_reason_codes=(
            ["accepted_severity_consistent"] if chief else []
        ),
        chief_evidence_references=(
            ["accepted-severity:High"] if chief else []
        ),
    )


def test_default_and_boundary_configuration():
    config = TaskOperatorRewardConfig()
    assert config.duplicates.top_k == 5
    assert config.chief_finder.bonus_percentage == Decimal("0.05")
    assert config.chief_finder.quality_percentage == Decimal("0.95")
    assert config.chief_finder.quality_threshold == Decimal("0.80")
    assert DuplicateRewardConfig(top_k=1).top_k == 1
    for value in (0, -1, 1001):
        with pytest.raises(ValidationError):
            DuplicateRewardConfig(top_k=value)
    for field in ("bonus_percentage", "quality_threshold"):
        for value in ("-0.000001", "1.000001"):
            with pytest.raises(ValidationError):
                ChiefFinderConfig(**{field: value})
    with pytest.raises(ValidationError):
        ChiefFinderConfig(
            bonus_percentage="0.10",
            quality_percentage="0.95",
        )
    assert ChiefFinderConfig(
        bonus_percentage="0.10"
    ).quality_percentage == Decimal("0.90")
    serialized = config.model_dump_json()
    assert serialized == TaskOperatorRewardConfig().model_dump_json()
    assert "membership" not in serialized
    assert "reputation" not in serialized


@pytest.mark.parametrize(
    ("quality", "expected"),
    [("1.000000", "1.000000000000"), ("0.500000", "0.250000000000"), ("0.900000", "0.810000000000")],
)
def test_quality_weight_is_exact_decimal_square(quality, expected):
    result = quality_weight(Decimal(quality))
    assert result == Decimal(expected)
    assert isinstance(result, Decimal)


def test_invalid_quality_weight_fails_without_clamping():
    for value in (Decimal("-0.1"), Decimal("1.1"), Decimal("NaN")):
        with pytest.raises(OperatorRewardCalculationError):
            quality_weight(value)


def test_best_report_per_operator_and_ties_are_deterministic():
    reports = [
        _report(3, "0.820000", operator="operator-a", node="node-c"),
        _report(2, "0.930000", operator="operator-a", node="node-b"),
        _report(1, "0.710000", operator="operator-a", node="node-a"),
    ]
    representatives, exclusions, _ = select_operator_representatives(reports)
    assert [item.submission_id for item in representatives] == ["submission-002"]
    assert len(exclusions) == 2
    assert all(
        item.reason == OperatorRewardExclusionReason.SUPERSEDED_BY_OPERATOR_BEST
        and item.representative_submission_id == "submission-002"
        for item in exclusions
    )

    tied = [
        _report(2, "0.900000", operator="operator-a", submitted_at=BASE_TIME),
        _report(1, "0.900000", operator="operator-a", submitted_at=BASE_TIME),
    ]
    assert select_operator_representatives(tied)[0][0].submission_id == "submission-001"


def test_single_chief_finder_receives_complete_cluster_reward():
    result = calculate_cluster_operator_payout(
        _day4(), [_report(1, "0.930000")], [], TaskOperatorRewardConfig()
    )
    allocation = result.operator_allocations[0]
    assert result.chief_bonus_pool_points == Decimal("50.000000")
    assert result.quality_pool_points == Decimal("950.000000")
    assert allocation.quality_reward_points == Decimal("950.000000")
    assert allocation.chief_bonus_points == Decimal("50.000000")
    assert allocation.total_reward_points == Decimal("1000.000000")
    assert result.distributed_points == result.cluster_reward_points


def test_no_chief_returns_complete_reward_to_quality_pool():
    result = calculate_cluster_operator_payout(
        _day4(), [_report(1, "0.790000", chief=False)], [], TaskOperatorRewardConfig()
    )
    assert result.chief_operator_id is None
    assert result.chief_bonus_pool_points == 0
    assert result.quality_pool_points == Decimal("1000.000000")
    assert result.operator_allocations[0].total_reward_points == Decimal("1000.000000")


def test_five_equal_quality_operators_split_quality_and_chief_bonus_exactly():
    reports = [_report(index, "0.900000") for index in range(1, 6)]
    result = calculate_cluster_operator_payout(
        _day4(), reports, [], TaskOperatorRewardConfig()
    )
    assert result.rewarded_operator_count == 5
    assert [item.quality_reward_points for item in result.operator_allocations] == [
        Decimal("190.000000")
    ] * 5
    assert result.operator_allocations[0].total_reward_points == Decimal("240.000000")
    assert all(
        item.total_reward_points == Decimal("190.000000")
        for item in result.operator_allocations[1:]
    )
    assert sum(item.total_reward_points for item in result.operator_allocations) == Decimal("1000.000000")


def test_more_than_top_k_has_ordinal_ranks_and_exactly_five_positive_recipients():
    reports = [_report(index, f"0.{1000-index:03d}000") for index in range(1, 51)]
    result = calculate_cluster_operator_payout(
        _day4(), list(reversed(reports)), [], TaskOperatorRewardConfig()
    )
    assert len(result.operator_allocations) == 50
    assert result.rewarded_operator_count == 5
    assert [item.quality_rank for item in result.operator_allocations] == list(range(1, 51))
    assert all(item.total_reward_points == 0 for item in result.operator_allocations[5:])
    assert all(
        item.exclusion_reason == OperatorRewardExclusionReason.OUTSIDE_TOP_K
        for item in result.operator_allocations[5:]
    )


def test_earliest_chief_can_differ_from_best_representative():
    early = _report(
        1, "0.820000", operator="operator-a", submitted_at=BASE_TIME
    )
    better = _report(
        2,
        "0.960000",
        operator="operator-a",
        submitted_at=BASE_TIME + timedelta(minutes=30),
    )
    competitor = _report(
        3,
        "0.980000",
        operator="operator-b",
        submitted_at=BASE_TIME + timedelta(minutes=45),
    )
    result = calculate_cluster_operator_payout(
        _day4(), [competitor, better, early], [], TaskOperatorRewardConfig()
    )
    operator_a = next(item for item in result.operator_allocations if item.operator_id == "operator-a")
    assert operator_a.rewarded_submission_id == better.submission_id
    assert operator_a.quality_score == Decimal("0.960000")
    assert operator_a.chief_finder
    assert operator_a.chief_qualifying_submission_id == early.submission_id
    assert result.operator_allocations[0].operator_id == "operator-b"
    assert result.operator_allocations[0].quality_rank == 1
    assert not result.operator_allocations[0].chief_finder


def test_early_low_quality_is_not_chief_and_later_better_report_is():
    early = _report(1, "0.450000", chief=False)
    later = _report(2, "0.940000")
    result = calculate_cluster_operator_payout(
        _day4(), [early, later], [], TaskOperatorRewardConfig()
    )
    assert result.chief_operator_id == later.operator_id


def test_earliest_qualifier_outside_top_k_cannot_be_chief_or_sixth_recipient():
    early_rank_six = _report(1, "0.810000", submitted_at=BASE_TIME)
    high = [
        _report(
            index,
            quality,
            submitted_at=BASE_TIME + timedelta(minutes=index),
        )
        for index, quality in enumerate(
            ["0.990000", "0.980000", "0.970000", "0.960000", "0.950000"],
            start=2,
        )
    ]
    result = calculate_cluster_operator_payout(
        _day4(), [early_rank_six, *high], [], TaskOperatorRewardConfig()
    )
    outside = next(
        item for item in result.operator_allocations if item.operator_id == early_rank_six.operator_id
    )
    assert outside.quality_rank == 6
    assert not outside.top_k_eligible
    assert not outside.chief_finder
    assert outside.total_reward_points == 0
    assert result.rewarded_operator_count == 5
    assert result.chief_operator_id == high[0].operator_id


def test_threshold_boundary_and_impact_evidence_apply_only_to_chief():
    below = _report(1, "0.799999")
    exact = _report(2, "0.800000", chief=False)
    result = calculate_cluster_operator_payout(
        _day4(), [below, exact], [], TaskOperatorRewardConfig()
    )
    assert result.chief_operator_id is None
    assert result.rewarded_operator_count == 2
    assert result.quality_pool_points == result.cluster_reward_points


@pytest.mark.parametrize(
    "failed_fact",
    [
        "chief_root_cause_qualified",
        "chief_severity_qualified",
        "chief_impact_qualified",
    ],
)
def test_each_authoritative_chief_qualification_fact_is_required(failed_fact):
    report = _report(1, "0.950000").model_copy(update={failed_fact: False})
    result = calculate_cluster_operator_payout(
        _day4(), [report], [], TaskOperatorRewardConfig()
    )
    assert result.chief_operator_id is None
    assert result.chief_bonus_pool_points == 0
    assert result.quality_pool_points == result.cluster_reward_points


def test_full_numerical_example_conserves_exact_cluster_reward():
    reports = [
        _report(index, quality)
        for index, quality in enumerate(
            ["0.900000", "0.950000", "0.850000", "0.700000"],
            start=1,
        )
    ]
    result = calculate_cluster_operator_payout(
        _day4("5559.850000"), reports, [], TaskOperatorRewardConfig()
    )
    assert result.chief_bonus_pool_points == Decimal("277.992500")
    assert result.quality_pool_points == Decimal("5281.857500")
    assert [item.quality_weight for item in result.operator_allocations] == [
        Decimal("0.902500000000"),
        Decimal("0.810000000000"),
        Decimal("0.722500000000"),
        Decimal("0.490000000000"),
    ]
    assert [item.total_reward_points for item in result.operator_allocations] == [
        Decimal("1629.701331"),
        Decimal("1740.660731"),
        Decimal("1304.663947"),
        Decimal("884.823991"),
    ]
    assert sum(
        item.total_reward_points for item in result.operator_allocations
    ) == Decimal("5559.850000")
    legacy_payload = result.model_dump()
    legacy_payload.pop("quality_pool_percentage")
    assert result.__class__.model_validate(legacy_payload) == result


def test_zero_quality_weight_is_safe_and_fully_undistributed():
    reports = [_report(1, "0.000000", chief=False), _report(2, "0.000000", chief=False)]
    result = calculate_cluster_operator_payout(
        _day4(), reports, [], TaskOperatorRewardConfig()
    )
    assert result.outcome.value == "no_positive_quality_weight"
    assert result.distributed_points == 0
    assert result.undistributed_points == result.cluster_reward_points


@pytest.mark.parametrize("percentage", ["0.000000", "1.000000"])
def test_chief_split_boundary_percentages_conserve_tiny_pool(percentage):
    chief, quality = split_chief_and_quality_pools(
        Decimal("0.000001"), True, Decimal(percentage)
    )
    assert chief + quality == Decimal("0.000001")
    result = calculate_cluster_operator_payout(
        _day4("0.000001"),
        [_report(1, "1.000000")],
        [],
        TaskOperatorRewardConfig(
            chief_finder=ChiefFinderConfig(
                bonus_percentage=percentage,
                quality_percentage=(Decimal("1") - Decimal(percentage)),
                quality_threshold="0.800000",
            )
        ),
    )
    assert result.distributed_points == Decimal("0.000001")


def test_repeated_and_reordered_inputs_produce_identical_logical_result_and_fingerprint():
    reports = [_report(index, f"0.{90-index}0000") for index in range(1, 8)]
    first = calculate_cluster_operator_payout(
        _day4("10.000001"), reports, [], TaskOperatorRewardConfig()
    )
    second = calculate_cluster_operator_payout(
        _day4("10.000001"), list(reversed(reports)), [], TaskOperatorRewardConfig()
    )
    assert first == second
    assert first.source_fingerprint == second.source_fingerprint
    assert first.distributed_points == Decimal("10.000001")
