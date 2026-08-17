from datetime import datetime, timezone
from decimal import Decimal

from app.schemas.task_finding_reward import (
    FindingAllocationScope,
    FindingClusterAllocationReason,
    FindingClusterRewardAllocation,
    FindingClusterValue,
    FindingParentPoolType,
    TaskFindingCalculationOutcome,
    TaskFindingCalculationStatus,
    TaskFindingRewardCalculation,
    TaskFindingRewardConfig,
)
from app.schemas.task_operator_reward import (
    TaskOperatorCalculationStatus,
    TaskOperatorRewardCalculation,
    TaskOperatorRewardConfig,
)
from app.schemas.week7_reward_cycle import Week7TaskRewardCycleCreateRequest
from app.services.task_finding_reward_calculator import (
    allocate_finding_cluster_pool,
    calculate_finding_score,
    calculate_uniqueness,
)
from app.services.task_operator_reward_calculator import (
    calculate_cluster_operator_payout,
)
from app.services.task_operator_reward_service import _operator_summaries
from app.services.week7_reward_cycle_service import (
    calculate_week7_task_reward_cycle,
    create_week7_task_reward_cycle,
    finalize_week7_task_reward_cycle,
    list_week7_reward_events,
)
from app.utils.protocol_serialization import protocol_fingerprint
from tests.test_task_operator_reward_service import _single_ready
from tests.test_task_operator_reward_simulation import _cluster_reports


def _synthetic_pipeline(cycle, budget):
    severity_data = [
        ("Critical", Decimal("16")),
        ("Critical", Decimal("16")),
        ("High", Decimal("8")),
        ("High", Decimal("8")),
        ("Medium", Decimal("3")),
        ("Medium", Decimal("3")),
        ("Medium", Decimal("3")),
        ("Low", Decimal("1")),
    ]
    config = TaskFindingRewardConfig()
    values = []
    for index, (severity, weight) in enumerate(severity_data):
        operator_count = min(40, 1 + index * 5)
        uniqueness = calculate_uniqueness(operator_count, config.uniqueness)
        values.append(
            FindingClusterValue(
                finding_cluster_id=f"cluster-{index}",
                project_id=cycle.project_id,
                routing_id=cycle.routing_id,
                category=("access_control" if index % 2 == 0 else "reentrancy"),
                cluster_source_fingerprint=f"{100 + index:064x}",
                final_severity=severity,
                severity_weight=weight,
                distinct_operator_count=operator_count,
                uniqueness=uniqueness,
                finding_score=calculate_finding_score(weight, uniqueness),
                allocation_reason=FindingClusterAllocationReason.POSITIVE_FINDING_SCORE,
            )
        )
    reward_by_cluster = allocate_finding_cluster_pool(
        budget.miner_pool_points, values
    )
    allocations = []
    for value in values:
        payload = {
            **value.model_dump(),
            "parent_pool_type": FindingParentPoolType.MINER_POOL,
            "parent_pool_id": budget.task_reward_budget_id,
            "parent_pool_points": budget.miner_pool_points,
            "cluster_reward_points": reward_by_cluster[value.finding_cluster_id],
        }
        allocations.append(
            FindingClusterRewardAllocation(
                **payload, source_fingerprint=protocol_fingerprint(payload)
            )
        )
    allocations.sort(key=lambda item: item.finding_cluster_id)
    now = datetime(2026, 8, 7, tzinfo=timezone.utc)
    day4_payload = {
        "task_reward_budget_id": budget.task_reward_budget_id,
        "allocations": allocations,
    }
    day4_fingerprint = protocol_fingerprint(day4_payload)
    day4 = TaskFindingRewardCalculation(
        calculation_id=f"task_finding_reward_calculation_{day4_fingerprint[:32]}",
        configuration_version="simulation-finding-config-v1",
        status=TaskFindingCalculationStatus.CALCULATED,
        outcome=TaskFindingCalculationOutcome.ALLOCATED,
        project_id=cycle.project_id,
        routing_id=cycle.routing_id,
        task_reward_budget_id=budget.task_reward_budget_id,
        task_reward_budget_source_fingerprint=budget.source_fingerprint,
        allocation_scope=FindingAllocationScope.GLOBAL,
        miner_pool_points=budget.miner_pool_points,
        validator_pool_points_snapshot=budget.validator_pool_points,
        protocol_pool_points_snapshot=budget.protocol_pool_points,
        severity_weights=config.severity_weights,
        uniqueness_config=config.uniqueness,
        category_weights={},
        category_pool_allocations=[],
        cluster_allocations=allocations,
        total_finding_score=sum(
            (value.finding_score for value in values), Decimal("0")
        ),
        eligible_cluster_count=8,
        positive_score_cluster_count=8,
        zero_weight_cluster_count=0,
        distributed_cluster_points=budget.miner_pool_points,
        undistributed_cluster_points=Decimal("0.000000"),
        source_fingerprint=day4_fingerprint,
        created_at=now,
        calculated_at=now,
    )

    operator_config = TaskOperatorRewardConfig()
    payouts = []
    for index, allocation in enumerate(allocations):
        reports = [] if index == 7 else _cluster_reports(index)
        payouts.append(
            calculate_cluster_operator_payout(
                allocation, reports, [], operator_config
            )
        )
    payouts.sort(key=lambda item: item.finding_cluster_id)
    distributed = sum((item.distributed_points for item in payouts), Decimal("0"))
    undistributed = sum(
        (item.undistributed_points for item in payouts), Decimal("0")
    )
    day5_payload = {"day4": day4_fingerprint, "payouts": payouts}
    day5_fingerprint = protocol_fingerprint(day5_payload)
    day5 = TaskOperatorRewardCalculation(
        calculation_id=f"task_operator_reward_calculation_{day5_fingerprint[:32]}",
        policy_version="operator_cluster_payout_v1",
        configuration_version="simulation-operator-config-v1",
        status=TaskOperatorCalculationStatus.CALCULATED,
        project_id=cycle.project_id,
        routing_id=cycle.routing_id,
        task_reward_budget_id=budget.task_reward_budget_id,
        day4_calculation_id=day4.calculation_id,
        day4_source_fingerprint=day4.source_fingerprint,
        source_cluster_reward_points=budget.miner_pool_points,
        cluster_payouts=payouts,
        operator_summaries=_operator_summaries(payouts),
        distributed_operator_points=distributed,
        undistributed_cluster_points=undistributed,
        source_fingerprint=day5_fingerprint,
        created_at=now,
        calculated_at=now,
    )
    return day4, day5, protocol_fingerprint(
        {"simulation": "week7-day6", "day4": day4, "day5": day5}
    )


def test_large_112_eligible_report_eight_cluster_finalization_simulation(
    tmp_path, monkeypatch
):
    root, workspace, routing, _, budget, _ = _single_ready(tmp_path)
    draft, _ = create_week7_task_reward_cycle(
        root,
        workspace,
        "project-1",
        routing.routing_id,
        Week7TaskRewardCycleCreateRequest(
            task_reward_budget_id=budget.task_reward_budget_id,
            allocation_scope=FindingAllocationScope.GLOBAL,
        ),
    )
    day4, day5, fingerprint = _synthetic_pipeline(draft, budget)

    def current_snapshot(_root, _cycle):
        return day4, day5, fingerprint

    monkeypatch.setattr(
        "app.services.week7_reward_cycle_service._calculate_current_snapshot",
        current_snapshot,
    )
    calculated, _ = calculate_week7_task_reward_cycle(
        root, "project-1", draft.reward_cycle_id
    )
    assert calculated.status.value == "calculated"
    assert list_week7_reward_events(root) == []
    finalized = finalize_week7_task_reward_cycle(
        root, "project-1", draft.reward_cycle_id
    ).cycle
    events = list_week7_reward_events(root)
    assert finalized.status.value == "finalized"
    assert len(events) == 35
    assert finalized.reward_event_count == len(events)
    assert all(
        len(
            [event for event in events if event.finding_cluster_id == cluster_id]
        )
        <= 5
        for cluster_id in {item.finding_cluster_id for item in day4.cluster_allocations}
    )
    for cluster_id in {event.finding_cluster_id for event in events}:
        cluster_events = [
            event for event in events if event.finding_cluster_id == cluster_id
        ]
        assert len({event.operator_id for event in cluster_events}) == len(cluster_events)
        assert sum(event.chief_finder for event in cluster_events) <= 1
    assert all(
        not event.chief_finder or event.quality_rank <= 5 for event in events
    )
    assert sum((event.total_reward_points for event in events), Decimal("0")) == finalized.distributed_miner_points
    assert finalized.distributed_miner_points + finalized.undistributed_miner_points == budget.miner_pool_points
    assert finalized.undistributed_miner_points == day4.cluster_allocations[7].cluster_reward_points
    assert finalized.validator_pool_points == budget.validator_pool_points
    assert finalized.protocol_pool_points == budget.protocol_pool_points
    repeated = finalize_week7_task_reward_cycle(
        root, "project-1", draft.reward_cycle_id
    )
    assert repeated.reward_events_created == 0
    assert repeated.reward_events_existing == 35
