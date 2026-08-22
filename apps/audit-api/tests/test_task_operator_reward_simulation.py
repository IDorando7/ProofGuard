import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.schemas.task_finding_reward import FindingClusterRewardAllocation
from app.schemas.task_operator_reward import EligibleReportRewardInput, TaskOperatorRewardConfig
from app.services.task_operator_reward_calculator import calculate_cluster_operator_payout
from tests.test_task_operator_reward_calculator import _day4, _report


def _cluster_day4(index):
    base = _day4(f"{1000 + index * 137}.000000")
    values = base.model_dump()
    values.update(
        finding_cluster_id=f"cluster-{index}",
        cluster_source_fingerprint=f"{100 + index:064x}",
        source_fingerprint=f"{200 + index:064x}",
    )
    return FindingClusterRewardAllocation.model_validate(values)


def _cluster_reports(index):
    reports = []
    base_time = datetime(2026, 3, 1, tzinfo=timezone.utc)
    for report_index in range(16):
        operator_index = (index * 5 + report_index % 10) % 40
        quality_units = 40 + ((index * 11 + report_index * 7) % 61)
        quality = Decimal(quality_units) / Decimal("100")
        original = _report(
            index * 100 + report_index + 1,
            f"{quality:.6f}",
            operator=f"operator-{operator_index:02d}",
            node=f"node-{(index * 16 + report_index) % 64:02d}",
            submitted_at=base_time
            + timedelta(minutes=index * 100 + report_index),
            chief=(report_index % 4 != 0),
        )
        values = original.model_dump()
        values.update(
            finding_cluster_id=f"cluster-{index}",
            submission_id=f"submission-{index}-{report_index:03d}",
            finding_id=f"finding-{index}-{report_index:03d}",
            routing_assignment_id=f"assignment-{index}-{report_index:03d}",
            assessment_id=f"assessment-{index}-{report_index:03d}",
            member_source_fingerprint=f"{1000 + index * 100 + report_index:064x}",
            assessment_source_fingerprint=f"{2000 + index * 100 + report_index:064x}",
        )
        reports.append(EligibleReportRewardInput.model_validate(values))
    return reports


def test_large_128_report_64_node_40_operator_eight_cluster_simulation():
    day4_allocations = [_cluster_day4(index) for index in range(8)]
    reports_by_cluster = {
        index: _cluster_reports(index) for index in range(8)
    }
    assert sum(len(items) for items in reports_by_cluster.values()) == 128
    assert len(
        {
            report.node_id
            for reports in reports_by_cluster.values()
            for report in reports
        }
    ) == 64
    assert len(
        {
            report.operator_id
            for reports in reports_by_cluster.values()
            for report in reports
        }
    ) == 40

    config = TaskOperatorRewardConfig()
    first = [
        calculate_cluster_operator_payout(
            allocation, reports_by_cluster[index], [], config
        )
        for index, allocation in enumerate(day4_allocations)
    ]
    randomized = []
    for index, allocation in reversed(list(enumerate(day4_allocations))):
        reports = list(reports_by_cluster[index])
        random.Random(9000 + index).shuffle(reports)
        randomized.append(
            calculate_cluster_operator_payout(allocation, reports, [], config)
        )
    randomized_by_id = {item.finding_cluster_id: item for item in randomized}
    assert {item.finding_cluster_id: item for item in first} == randomized_by_id

    source_total = sum(item.cluster_reward_points for item in first)
    distributed = sum(item.distributed_points for item in first)
    undistributed = sum(item.undistributed_points for item in first)
    assert distributed + undistributed == source_total
    assert all(item.rewarded_operator_count <= 5 for item in first)
    assert all(item.distributed_points <= item.cluster_reward_points for item in first)
    for payout in first:
        rewarded = [item.operator_id for item in payout.operator_allocations if item.rewarded]
        assert len(rewarded) == len(set(rewarded))
        assert payout.chief_operator_id is None or payout.chief_operator_id in rewarded
        assert all(
            item.chief_finder is False
            for item in payout.operator_allocations
            if not item.top_k_eligible
        )
    rewarded_by_operator = {}
    for payout in first:
        for allocation in payout.operator_allocations:
            if allocation.rewarded:
                rewarded_by_operator.setdefault(allocation.operator_id, set()).add(
                    payout.finding_cluster_id
                )
    assert any(len(cluster_ids) > 1 for cluster_ids in rewarded_by_operator.values())


def test_cluster_and_category_value_never_moves_when_one_cluster_is_undistributed():
    empty = calculate_cluster_operator_payout(
        _cluster_day4(0), [], [], TaskOperatorRewardConfig()
    )
    populated = calculate_cluster_operator_payout(
        _cluster_day4(1), _cluster_reports(1), [], TaskOperatorRewardConfig()
    )
    assert empty.distributed_points == 0
    assert empty.undistributed_points == empty.cluster_reward_points
    assert populated.distributed_points == populated.cluster_reward_points
    assert populated.distributed_points == _cluster_day4(1).cluster_reward_points
