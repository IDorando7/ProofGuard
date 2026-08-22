from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.finding import FindingSeverity
from app.schemas.task_finding_reward import (
    FindingClusterAllocationReason,
    FindingClusterValue,
    SeverityRewardWeightConfig,
    TaskFindingRewardConfig,
    UniquenessRewardConfig,
)
from app.services.task_finding_reward_calculator import (
    FindingValuationError,
    allocate_finding_cluster_pool,
    calculate_finding_score,
    calculate_uniqueness,
    severity_weight,
)


def _value(identifier: str, score: str):
    return FindingClusterValue(
        finding_cluster_id=identifier,
        project_id="project-1",
        routing_id="routing-1",
        category="access_control",
        cluster_source_fingerprint="a" * 64,
        final_severity="High",
        severity_weight=score,
        distinct_operator_count=1,
        uniqueness="1.000000",
        finding_score=score,
        allocation_reason=(
            FindingClusterAllocationReason.POSITIVE_FINDING_SCORE
            if Decimal(score) > 0
            else FindingClusterAllocationReason.ZERO_SEVERITY_WEIGHT
        ),
    )


def test_default_configuration_and_explicit_informational_policy():
    config = TaskFindingRewardConfig()
    assert config.severity_weights.model_dump() == {
        "critical": Decimal("16"),
        "high": Decimal("8"),
        "medium": Decimal("3"),
        "low": Decimal("1"),
        "informational": Decimal("0"),
    }
    assert config.uniqueness.coefficient == Decimal("0.20")
    assert config.uniqueness.floor == Decimal("0.50")
    assert config.default_allocation_scope.value == "category_isolated"
    assert not hasattr(config, "quality")
    assert not hasattr(config, "top_k")


@pytest.mark.parametrize(
    ("severity", "expected"),
    [
        (FindingSeverity.CRITICAL, "16"),
        (FindingSeverity.HIGH, "8"),
        (FindingSeverity.MEDIUM, "3"),
        (FindingSeverity.LOW, "1"),
        (FindingSeverity.INFORMATIONAL, "0"),
    ],
)
def test_severity_weights_are_explicit(severity, expected):
    assert severity_weight(severity, SeverityRewardWeightConfig()) == Decimal(expected)


def test_unknown_severity_and_invalid_configuration_fail():
    with pytest.raises(FindingValuationError, match="Unknown"):
        severity_weight("SuperCritical", SeverityRewardWeightConfig())
    with pytest.raises(ValidationError):
        SeverityRewardWeightConfig(high="-1")
    with pytest.raises(ValidationError):
        UniquenessRewardConfig(coefficient="-0.1")
    with pytest.raises(ValidationError):
        UniquenessRewardConfig(floor="-0.1")
    with pytest.raises(ValidationError):
        UniquenessRewardConfig(floor="1.1")


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (1, "1.000000"),
        (2, "0.878249"),
        (5, "0.756494"),
        (10, "0.684689"),
        (50, "0.561040"),
        (100, "0.520553"),
        (149, "0.500000"),
        (1000, "0.500000"),
    ],
)
def test_uniqueness_exact_default_values(count, expected):
    value = calculate_uniqueness(count, UniquenessRewardConfig())
    assert value == Decimal(expected)
    assert Decimal("0.500000") <= value <= Decimal("1.000000")


def test_uniqueness_is_monotone_deterministic_and_safe_for_huge_counts():
    config = UniquenessRewardConfig()
    values = [calculate_uniqueness(count, config) for count in range(1, 300)]
    assert values == sorted(values, reverse=True)
    assert calculate_uniqueness(10**1000, config) == Decimal("0.500000")
    assert calculate_uniqueness(100, config) == calculate_uniqueness(100, config)
    with pytest.raises(FindingValuationError):
        calculate_uniqueness(0, config)
    with pytest.raises(FindingValuationError):
        calculate_uniqueness(-1, config)
    for invalid in (True, 1.5, Decimal("2"), "2"):
        with pytest.raises(FindingValuationError, match="positive integer"):
            calculate_uniqueness(invalid, config)


def test_zero_coefficient_means_full_uniqueness_for_every_positive_count():
    config = UniquenessRewardConfig(coefficient="0", floor="0.50")
    assert calculate_uniqueness(1_000_000, config) == Decimal("1.000000")


@pytest.mark.parametrize(
    ("weight", "uniqueness", "expected"),
    [
        ("16", "1.000000", "16.000000"),
        ("8", "1.000000", "8.000000"),
        ("3", "1.000000", "3.000000"),
        ("1", "1.000000", "1.000000"),
        ("16", "0.500000", "8.000000"),
        ("8", "0.878248", "7.025984"),
    ],
)
def test_finding_score_uses_persisted_uniqueness(weight, uniqueness, expected):
    assert calculate_finding_score(
        Decimal(weight), Decimal(uniqueness)
    ) == Decimal(expected)


def test_global_allocator_reuses_exact_largest_remainder_and_zero_score_exclusion():
    values = [_value("cluster-a", "1.000000"), _value("cluster-b", "1.000000")]
    assert allocate_finding_cluster_pool(Decimal("1.000001"), values) == {
        "cluster-a": Decimal("0.500001"),
        "cluster-b": Decimal("0.500000"),
    }
    with_zero = values + [_value("cluster-c", "0.000000")]
    allocation = allocate_finding_cluster_pool(Decimal("10.000001"), with_zero)
    assert allocation["cluster-c"] == 0
    assert sum(allocation.values()) == Decimal("10.000001")
    assert allocation == allocate_finding_cluster_pool(
        Decimal("10.000001"), list(reversed(with_zero))
    )


def test_empty_and_all_zero_parent_pool_remain_undistributed():
    assert allocate_finding_cluster_pool(Decimal("1.000000"), []) == {}
    assert allocate_finding_cluster_pool(
        Decimal("1.000000"), [_value("cluster-a", "0.000000")]
    ) == {"cluster-a": Decimal("0.000000")}


def test_global_three_cluster_benchmark_is_exact_and_conservative():
    config = TaskFindingRewardConfig()
    cases = [
        ("cluster-a", FindingSeverity.HIGH, 1),
        ("cluster-b", FindingSeverity.CRITICAL, 4),
        ("cluster-c", FindingSeverity.MEDIUM, 12),
    ]
    values = []
    for identifier, severity, operator_count in cases:
        weight = severity_weight(severity, config.severity_weights)
        uniqueness = calculate_uniqueness(operator_count, config.uniqueness)
        score = calculate_finding_score(weight, uniqueness)
        values.append(
            FindingClusterValue(
                finding_cluster_id=identifier,
                project_id="project-1",
                routing_id="routing-1",
                category="access_control",
                cluster_source_fingerprint="a" * 64,
                final_severity=severity,
                severity_weight=weight,
                distinct_operator_count=operator_count,
                uniqueness=uniqueness,
                finding_score=score,
                allocation_reason="positive_finding_score",
            )
        )

    assert [value.finding_score for value in values] == [
        Decimal("8.000000"),
        Decimal("12.526832"),
        Decimal("2.004033"),
    ]
    rewards = allocate_finding_cluster_pool(Decimal("10000.000000"), values)
    assert rewards == {
        "cluster-a": Decimal("3550.684805"),
        "cluster-b": Decimal("5559.854005"),
        "cluster-c": Decimal("889.461190"),
    }
    assert rewards["cluster-b"] > rewards["cluster-a"] > rewards["cluster-c"]
    assert sum(rewards.values(), Decimal("0")) == Decimal("10000.000000")
    assert allocate_finding_cluster_pool(
        Decimal("10000.000000"), list(reversed(values))
    ) == rewards
