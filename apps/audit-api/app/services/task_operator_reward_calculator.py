from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from app.schemas.task_finding_reward import FindingClusterRewardAllocation
from app.schemas.task_operator_reward import (
    ClusterPayoutOutcome,
    EligibleReportRewardInput,
    FindingClusterOperatorPayoutCalculation,
    OperatorClusterRewardAllocation,
    OperatorRewardExclusionReason,
    ReportRewardExclusion,
    TaskOperatorRewardConfig,
    TASK_OPERATOR_POLICY_VERSION,
)
from app.services.subnet_reward_allocation_service import (
    WeightedRewardItem,
    allocate_proportional_rewards,
)
from app.utils.protocol_serialization import protocol_fingerprint


class OperatorRewardCalculationError(ValueError):
    pass


def quality_weight(quality_score: Decimal) -> Decimal:
    if not quality_score.is_finite() or quality_score < 0 or quality_score > 1:
        raise OperatorRewardCalculationError(
            "Finalized report quality must be between zero and one"
        )
    return quality_score * quality_score


def select_operator_representatives(
    reports: list[EligibleReportRewardInput],
) -> tuple[
    list[EligibleReportRewardInput],
    list[ReportRewardExclusion],
    dict[str, list[EligibleReportRewardInput]],
]:
    """Choose one best report per operator and preserve every losing report."""
    grouped: dict[str, list[EligibleReportRewardInput]] = defaultdict(list)
    for report in reports:
        grouped[report.operator_id].append(report)
    representatives: list[EligibleReportRewardInput] = []
    exclusions: list[ReportRewardExclusion] = []
    ordered_groups: dict[str, list[EligibleReportRewardInput]] = {}
    for operator_id in sorted(grouped):
        ordered = sorted(
            grouped[operator_id],
            key=lambda item: (
                -item.quality_score,
                item.submitted_at,
                item.submission_id,
                item.node_id,
            ),
        )
        representative = ordered[0]
        representatives.append(representative)
        ordered_groups[operator_id] = ordered
        exclusions.extend(
            ReportRewardExclusion(
                finding_cluster_id=report.finding_cluster_id,
                submission_id=report.submission_id,
                node_id=report.node_id,
                operator_id=report.operator_id,
                quality_score=report.quality_score,
                reason=OperatorRewardExclusionReason.SUPERSEDED_BY_OPERATOR_BEST,
                representative_submission_id=representative.submission_id,
                quality_assessment_id=report.assessment_id,
            )
            for report in ordered[1:]
        )
    representatives.sort(
        key=lambda item: (
            -item.quality_score,
            item.submitted_at,
            item.submission_id,
            item.operator_id,
        )
    )
    return representatives, exclusions, ordered_groups


def split_chief_and_quality_pools(
    cluster_reward_points: Decimal,
    chief_exists: bool,
    bonus_percentage: Decimal,
) -> tuple[Decimal, Decimal]:
    """Split exactly with the existing largest-remainder accounting helper."""
    if not chief_exists:
        return Decimal("0.000000"), cluster_reward_points
    shares = [
        ("0_chief", bonus_percentage),
        ("1_quality", Decimal("1") - bonus_percentage),
    ]
    allocations = allocate_proportional_rewards(
        cluster_reward_points,
        [
            WeightedRewardItem(
                allocation_id=identifier,
                node_id=identifier,
                submission_id=identifier,
                raw_weight=weight,
                contribution_score=Decimal("0"),
            )
            for identifier, weight in shares
            if weight > 0
        ],
    )
    chief = allocations.get("0_chief", Decimal("0.000000"))
    return chief, cluster_reward_points - chief


def calculate_cluster_operator_payout(
    day4_allocation: FindingClusterRewardAllocation,
    reports: list[EligibleReportRewardInput],
    initial_exclusions: list[ReportRewardExclusion],
    config: TaskOperatorRewardConfig,
    *,
    policy_version: str = TASK_OPERATOR_POLICY_VERSION,
) -> FindingClusterOperatorPayoutCalculation:
    reports = sorted(reports, key=lambda item: (item.submitted_at, item.submission_id))
    if any(
        report.finding_cluster_id != day4_allocation.finding_cluster_id
        for report in reports
    ):
        raise OperatorRewardCalculationError(
            "Eligible reports cannot cross FindingCluster boundaries"
        )
    representatives, exclusions, grouped = select_operator_representatives(reports)
    top_k = representatives[: config.duplicates.top_k]
    top_operator_ids = {item.operator_id for item in top_k}

    chief_candidates: list[EligibleReportRewardInput] = []
    chief_by_operator: dict[str, EligibleReportRewardInput] = {}
    for operator_id in sorted(top_operator_ids):
        qualifying = sorted(
            (
                report
                for report in grouped[operator_id]
                if day4_allocation.cluster_reward_points > 0
                and quality_weight(report.quality_score) > 0
                and report.quality_score >= config.chief_finder.quality_threshold
                and report.chief_root_cause_qualified
                and report.chief_impact_qualified
            ),
            key=lambda item: (item.submitted_at, item.submission_id),
        )
        if qualifying:
            chief_by_operator[operator_id] = qualifying[0]
            chief_candidates.append(qualifying[0])
    chief_candidates.sort(
        key=lambda item: (item.submitted_at, item.submission_id, item.operator_id)
    )
    chief_report = chief_candidates[0] if chief_candidates else None
    chief_operator_id = chief_report.operator_id if chief_report else None

    chief_pool, quality_pool = split_chief_and_quality_pools(
        day4_allocation.cluster_reward_points,
        chief_report is not None,
        config.chief_finder.bonus_percentage,
    )
    weights = {
        report.operator_id: quality_weight(report.quality_score)
        for report in representatives
    }
    positive = [report for report in top_k if weights[report.operator_id] > 0]
    quality_rewards: dict[str, Decimal] = {}
    if positive:
        quality_rewards = allocate_proportional_rewards(
            quality_pool,
            [
                WeightedRewardItem(
                    allocation_id=report.operator_id,
                    node_id=report.operator_id,
                    submission_id=report.submission_id,
                    raw_weight=weights[report.operator_id],
                    contribution_score=Decimal("0"),
                )
                for report in positive
            ],
        )

    allocations: list[OperatorClusterRewardAllocation] = []
    for rank, representative in enumerate(representatives, start=1):
        is_top_k = representative.operator_id in top_operator_ids
        is_chief = representative.operator_id == chief_operator_id
        quality_points = quality_rewards.get(
            representative.operator_id, Decimal("0.000000")
        )
        bonus_points = chief_pool if is_chief else Decimal("0.000000")
        total = quality_points + bonus_points
        reason = None
        if not is_top_k:
            reason = OperatorRewardExclusionReason.OUTSIDE_TOP_K
        elif weights[representative.operator_id] == 0:
            reason = OperatorRewardExclusionReason.NO_POSITIVE_QUALITY_WEIGHT
        elif total == 0:
            reason = OperatorRewardExclusionReason.ZERO_QUALITY_POOL
        qualifying = chief_by_operator.get(representative.operator_id)
        allocations.append(
            OperatorClusterRewardAllocation(
                finding_cluster_id=day4_allocation.finding_cluster_id,
                operator_id=representative.operator_id,
                rewarded_node_id=representative.node_id,
                rewarded_submission_id=representative.submission_id,
                quality_assessment_id=representative.assessment_id,
                quality_score=representative.quality_score,
                quality_weight=weights[representative.operator_id],
                quality_rank=rank,
                top_k_eligible=is_top_k,
                rewarded=total > 0,
                chief_finder=is_chief,
                chief_qualifying_submission_id=(
                    qualifying.submission_id if qualifying else None
                ),
                chief_qualifying_submitted_at=(
                    qualifying.submitted_at if qualifying else None
                ),
                quality_reward_points=quality_points,
                chief_bonus_points=bonus_points,
                total_reward_points=total,
                exclusion_reason=reason,
                calculation_policy_version=policy_version,
            )
        )
        if not is_top_k:
            exclusions.append(
                ReportRewardExclusion(
                    finding_cluster_id=representative.finding_cluster_id,
                    submission_id=representative.submission_id,
                    node_id=representative.node_id,
                    operator_id=representative.operator_id,
                    quality_score=representative.quality_score,
                    reason=OperatorRewardExclusionReason.OUTSIDE_TOP_K,
                    representative_submission_id=representative.submission_id,
                    quality_assessment_id=representative.assessment_id,
                )
            )
        elif weights[representative.operator_id] == 0:
            exclusions.append(
                ReportRewardExclusion(
                    finding_cluster_id=representative.finding_cluster_id,
                    submission_id=representative.submission_id,
                    node_id=representative.node_id,
                    operator_id=representative.operator_id,
                    quality_score=representative.quality_score,
                    reason=OperatorRewardExclusionReason.NO_POSITIVE_QUALITY_WEIGHT,
                    representative_submission_id=representative.submission_id,
                    quality_assessment_id=representative.assessment_id,
                )
            )

    exclusions = sorted(
        [*initial_exclusions, *exclusions],
        key=lambda item: (item.submission_id, item.reason.value),
    )
    distributed = sum(
        (allocation.total_reward_points for allocation in allocations), Decimal("0")
    )
    undistributed = day4_allocation.cluster_reward_points - distributed
    if not reports:
        outcome = ClusterPayoutOutcome.NO_ELIGIBLE_REPORTS
    elif not positive:
        outcome = ClusterPayoutOutcome.NO_POSITIVE_QUALITY_WEIGHT
    else:
        outcome = ClusterPayoutOutcome.ALLOCATED
    total_weight = sum((weights[item.operator_id] for item in top_k), Decimal("0"))
    payload = {
        "policy_version": policy_version,
        "day4_allocation": day4_allocation,
        "top_k": config.duplicates.top_k,
        "chief_finder": config.chief_finder,
        "eligible_reports": reports,
        "operator_allocations": allocations,
        "report_exclusions": exclusions,
        "chief_operator_id": chief_operator_id,
        "chief_qualifying_submission_id": (
            chief_report.submission_id if chief_report else None
        ),
        "chief_bonus_pool_points": chief_pool,
        "quality_pool_points": quality_pool,
        "total_quality_weight": total_weight,
        "distributed_points": distributed,
        "undistributed_points": undistributed,
        "outcome": outcome,
    }
    return FindingClusterOperatorPayoutCalculation(
        finding_cluster_id=day4_allocation.finding_cluster_id,
        project_id=day4_allocation.project_id,
        routing_id=day4_allocation.routing_id,
        category=day4_allocation.category,
        finding_cluster_source_fingerprint=day4_allocation.cluster_source_fingerprint,
        day4_allocation_source_fingerprint=day4_allocation.source_fingerprint,
        cluster_reward_points=day4_allocation.cluster_reward_points,
        eligible_report_count=len(reports),
        distinct_eligible_operator_count=len(representatives),
        top_k_limit=config.duplicates.top_k,
        rewarded_operator_count=sum(item.rewarded for item in allocations),
        chief_bonus_percentage=config.chief_finder.bonus_percentage,
        chief_quality_threshold=config.chief_finder.quality_threshold,
        chief_operator_id=chief_operator_id,
        chief_qualifying_submission_id=(
            chief_report.submission_id if chief_report else None
        ),
        chief_bonus_pool_points=chief_pool,
        quality_pool_points=quality_pool,
        total_quality_weight=total_weight,
        operator_allocations=allocations,
        report_exclusions=exclusions,
        distributed_points=distributed,
        undistributed_points=undistributed,
        outcome=outcome,
        policy_version=policy_version,
        source_fingerprint=protocol_fingerprint(payload),
    )
