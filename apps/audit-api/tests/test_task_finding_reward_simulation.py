from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.schemas.finding import FindingSeverity
from app.schemas.finding_cluster import (
    FindingCluster,
    FindingClusterMember,
    FindingClusterMemberRelation,
    FindingClusterStatus,
)
from app.schemas.task_finding_reward import TaskFindingRewardConfig
from app.services.subnet_reward_allocation_service import allocate_category_pools
from app.services.task_finding_reward_calculator import (
    allocate_finding_cluster_pool,
    value_finding_cluster,
)


def _cluster(index, severity, operator_count, category):
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    extra_same_operator_nodes = min(index, 3)
    report_count = operator_count + extra_same_operator_nodes
    members = []
    for member_index in range(report_count):
        operator_index = member_index if member_index < operator_count else 0
        members.append(
            FindingClusterMember(
                submission_id=f"submission-{index}-{member_index}",
                finding_id=f"finding-{index}-{member_index}",
                node_id=f"node-{(index * 13 + member_index) % 64}",
                operator_id=f"operator-{operator_index % 40}",
                validation_id=f"validation-{index}-{member_index}",
                reproduction_id=f"reproduction-{index}-{member_index}",
                submitted_at=base + timedelta(seconds=index * 1000 + member_index),
                relation=(
                    FindingClusterMemberRelation.CANONICAL
                    if member_index == 0
                    else FindingClusterMemberRelation.INDEPENDENT_DUPLICATE
                ),
                source_fingerprint=f"{index + member_index + 1:064x}"[-64:],
            )
        )
    cluster_fingerprint = f"{1000 + index:064x}"[-64:]
    return FindingCluster(
        finding_cluster_id=f"finding_cluster_{index + 1:064x}",
        project_id="simulation-project",
        routing_id="simulation-routing",
        category=category,
        canonical_finding_id=members[0].finding_id,
        canonical_submission_id=members[0].submission_id,
        final_severity=severity,
        root_cause_key=f"root-cause-{index}",
        root_cause_fingerprint=f"{2000 + index:064x}"[-64:],
        members=members,
        report_count=report_count,
        distinct_operator_count=operator_count,
        status=FindingClusterStatus.FINALIZED,
        source_fingerprint=cluster_fingerprint,
        created_at=base,
        updated_at=base,
        finalized_at=base,
    )


def test_large_64_node_40_operator_eight_cluster_simulation_is_deterministic():
    severities = [
        FindingSeverity.CRITICAL,
        FindingSeverity.CRITICAL,
        FindingSeverity.HIGH,
        FindingSeverity.HIGH,
        FindingSeverity.MEDIUM,
        FindingSeverity.MEDIUM,
        FindingSeverity.MEDIUM,
        FindingSeverity.LOW,
    ]
    operator_counts = [1, 2, 4, 7, 12, 20, 30, 40]
    categories = ["access_control"] * 4 + ["reentrancy"] * 4
    clusters = [
        _cluster(index, severity, operator_count, category)
        for index, (severity, operator_count, category) in enumerate(
            zip(severities, operator_counts, categories, strict=True)
        )
    ]
    assert len({member.node_id for cluster in clusters for member in cluster.members}) == 64
    assert max(cluster.report_count for cluster in clusters) > max(operator_counts)

    config = TaskFindingRewardConfig()
    first_values = [value_finding_cluster(cluster, config) for cluster in clusters]
    second_values = [
        value_finding_cluster(cluster, config) for cluster in reversed(clusters)
    ]
    first_by_id = {value.finding_cluster_id: value for value in first_values}
    second_by_id = {value.finding_cluster_id: value for value in second_values}
    assert first_by_id == second_by_id
    assert [value.distinct_operator_count for value in first_values] == operator_counts
    assert all(Decimal("0.500000") <= value.uniqueness <= 1 for value in first_values)
    assert all(
        first_values[index].uniqueness >= first_values[index + 1].uniqueness
        for index in range(len(first_values) - 1)
    )

    miner_pool = Decimal("7000.000000")
    category_pools = allocate_category_pools(
        miner_pool, ["access_control", "reentrancy"], None
    )
    allocations = {}
    for category in ("access_control", "reentrancy"):
        scoped = [value for value in first_values if value.category.value == category]
        allocations.update(
            allocate_finding_cluster_pool(category_pools[category], scoped)
        )
    assert len(allocations) == 8
    assert sum(allocations.values()) == miner_pool
    repeated = {}
    for category in ("reentrancy", "access_control"):
        scoped = [value for value in reversed(first_values) if value.category.value == category]
        repeated.update(allocate_finding_cluster_pool(category_pools[category], scoped))
    assert repeated == allocations
    global_allocations = allocate_finding_cluster_pool(miner_pool, first_values)
    assert len(global_allocations) == 8
    assert sum(global_allocations.values()) == miner_pool
    assert global_allocations == allocate_finding_cluster_pool(
        miner_pool, list(reversed(first_values))
    )
    assert not hasattr(first_values[0], "quality_score")
    assert not hasattr(first_values[0], "operator_reward")
    assert not hasattr(first_values[0], "quality_rank")


def test_category_isolation_keeps_empty_pool_undistributed_and_explicit_weights():
    pools = allocate_category_pools(
        Decimal("10000.000000"),
        ["access_control", "reentrancy"],
        {"access_control": Decimal("3"), "reentrancy": Decimal("2")},
    )
    assert pools == {
        "access_control": Decimal("6000.000000"),
        "reentrancy": Decimal("4000.000000"),
    }
    values = [
        value_finding_cluster(
            _cluster(0, FindingSeverity.HIGH, 1, "access_control"),
            TaskFindingRewardConfig(),
        )
    ]
    distributed = allocate_finding_cluster_pool(pools["access_control"], values)
    empty = allocate_finding_cluster_pool(pools["reentrancy"], [])
    assert sum(distributed.values()) == Decimal("6000.000000")
    assert empty == {}
    assert Decimal("4000.000000") == pools["reentrancy"]
