from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from app.schemas.finding import FindingSeverity
from app.schemas.finding_cluster import FindingCluster
from app.schemas.task_finding_reward import (
    FINDING_SCORE_QUANTUM,
    UNIQUENESS_QUANTUM,
    FindingClusterAllocationReason,
    FindingClusterValue,
    SeverityRewardWeightConfig,
    TaskFindingRewardConfig,
    UniquenessRewardConfig,
)
from app.services.subnet_reward_allocation_service import (
    WeightedRewardItem,
    allocate_proportional_rewards,
)


class FindingValuationError(ValueError):
    """A cluster cannot be valued under the configured deterministic policy."""


def severity_weight(
    severity: FindingSeverity | str,
    config: SeverityRewardWeightConfig,
) -> Decimal:
    """Return the explicit configured weight; unknown severities never fall back."""
    try:
        normalized = FindingSeverity(severity)
    except ValueError as exc:
        raise FindingValuationError(f"Unknown finding severity: {severity}") from exc
    return {
        FindingSeverity.CRITICAL: config.critical,
        FindingSeverity.HIGH: config.high,
        FindingSeverity.MEDIUM: config.medium,
        FindingSeverity.LOW: config.low,
        FindingSeverity.INFORMATIONAL: config.informational,
    }[normalized]


def calculate_uniqueness(
    distinct_operator_count: int,
    config: UniquenessRewardConfig,
) -> Decimal:
    """Calculate max(floor, 1/(1+coefficient*ln(N))) using Decimal only."""
    if type(distinct_operator_count) is not int or distinct_operator_count < 1:
        raise FindingValuationError(
            "Distinct operator count must be a positive integer"
        )
    if config.coefficient == 0 or distinct_operator_count == 1:
        return Decimal("1.000000")
    with localcontext() as context:
        context.prec = 50
        count = Decimal(distinct_operator_count)
        raw = Decimal("1") / (
            Decimal("1") + config.coefficient * count.ln()
        )
        bounded = max(config.floor, min(Decimal("1"), raw))
        return bounded.quantize(UNIQUENESS_QUANTUM, rounding=ROUND_HALF_EVEN)


def calculate_finding_score(
    configured_severity_weight: Decimal,
    persisted_uniqueness: Decimal,
) -> Decimal:
    """Use the same six-decimal uniqueness that is persisted and exposed."""
    if not configured_severity_weight.is_finite() or configured_severity_weight < 0:
        raise FindingValuationError("Severity weight must be finite and non-negative")
    if (
        not persisted_uniqueness.is_finite()
        or persisted_uniqueness < 0
        or persisted_uniqueness > 1
        or persisted_uniqueness
        != persisted_uniqueness.quantize(UNIQUENESS_QUANTUM)
    ):
        raise FindingValuationError(
            "Persisted uniqueness must be a six-decimal value between zero and one"
        )
    return (configured_severity_weight * persisted_uniqueness).quantize(
        FINDING_SCORE_QUANTUM, rounding=ROUND_HALF_EVEN
    )


def value_finding_cluster(
    cluster: FindingCluster,
    config: TaskFindingRewardConfig,
) -> FindingClusterValue:
    operator_count = len({member.operator_id for member in cluster.members})
    if operator_count != cluster.distinct_operator_count:
        raise FindingValuationError(
            "FindingCluster distinct operator count does not match valid members"
        )
    weight = severity_weight(cluster.final_severity, config.severity_weights)
    uniqueness = calculate_uniqueness(operator_count, config.uniqueness)
    score = calculate_finding_score(weight, uniqueness)
    return FindingClusterValue(
        finding_cluster_id=cluster.finding_cluster_id,
        project_id=cluster.project_id,
        routing_id=cluster.routing_id,
        category=cluster.category,
        cluster_source_fingerprint=cluster.source_fingerprint,
        final_severity=cluster.final_severity,
        severity_weight=weight,
        distinct_operator_count=operator_count,
        uniqueness=uniqueness,
        finding_score=score,
        allocation_reason=(
            FindingClusterAllocationReason.POSITIVE_FINDING_SCORE
            if score > 0
            else FindingClusterAllocationReason.ZERO_SEVERITY_WEIGHT
        ),
    )


def allocate_finding_cluster_pool(
    parent_pool_points: Decimal,
    values: list[FindingClusterValue],
) -> dict[str, Decimal]:
    """Allocate one parent pool with the reused Week 6 largest-remainder helper."""
    if len({value.finding_cluster_id for value in values}) != len(values):
        raise FindingValuationError("FindingCluster values must be unique")
    positive = [value for value in values if value.finding_score > 0]
    rewards = allocate_proportional_rewards(
        parent_pool_points,
        [
            WeightedRewardItem(
                allocation_id=value.finding_cluster_id,
                node_id=value.finding_cluster_id,
                submission_id=value.finding_cluster_id,
                raw_weight=value.finding_score,
                contribution_score=Decimal("0"),
            )
            for value in positive
        ],
    )
    return {
        value.finding_cluster_id: rewards.get(
            value.finding_cluster_id, Decimal("0.000000")
        )
        for value in sorted(values, key=lambda item: item.finding_cluster_id)
    }
