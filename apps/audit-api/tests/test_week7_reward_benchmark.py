import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.report_quality import ReportQualityAssessmentRequest
from app.schemas.task_operator_reward import TaskOperatorRewardConfig
from app.schemas.week7_reward_cycle import Week7TaskRewardCycleCreateRequest
from app.services.subnet_reward_allocation_service import (
    WeightedRewardItem,
    allocate_proportional_rewards,
)
from app.services.task_finding_reward_calculator import calculate_uniqueness
from app.schemas.task_finding_reward import UniquenessRewardConfig
from app.services.task_operator_reward_calculator import quality_weight
from research.benchmarks.week7_reward_benchmark import (
    _large_top_k_stress,
    run_week7_reward_benchmark,
)
from research.reports.week7_reward_report import REQUIRED_REPORT_SECTIONS
from research.schemas.week7_reward_benchmark import GENERATED_FILENAMES, REQUIRED_CASE_IDS


def test_full_week7_real_service_benchmark_generates_passing_auditable_outputs(tmp_path):
    cases, summary, outputs = run_week7_reward_benchmark(tmp_path / "results")
    assert tuple(item.case_id for item in cases.cases) == REQUIRED_CASE_IDS
    assert all(item.passed for item in cases.cases)
    assert summary.benchmark_passed
    assert summary.nodes == 64
    assert summary.operators == 40
    assert summary.valid_submissions >= 100
    assert summary.finding_clusters == 8
    assert summary.reward_events > 0
    assert Decimal(summary.distributed_miner_amount) > 0
    assert Decimal(summary.distributed_miner_amount) + Decimal(
        summary.undistributed_miner_amount
    ) == Decimal(summary.miner_pool)
    assert all(outputs["invariants"].values())
    for filename in GENERATED_FILENAMES:
        path = tmp_path / "results" / filename
        assert path.is_file()
        text = path.read_text(encoding="utf-8")
        assert "/home/" not in text
        assert "private_key" not in text
    report = (tmp_path / "results" / "week7_report.md").read_text(encoding="utf-8")
    assert all(section in report for section in REQUIRED_REPORT_SECTIONS)


@pytest.mark.parametrize("count", list(range(1, 300)) + [500, 1000, 10**6])
def test_uniqueness_property_is_bounded_monotone_and_floored(count):
    config = UniquenessRewardConfig()
    current = calculate_uniqueness(count, config)
    following = calculate_uniqueness(count + 1, config)
    assert Decimal("0.500000") <= current <= Decimal("1.000000")
    assert following <= current
    if count >= 149:
        assert current == Decimal("0.500000")


@pytest.mark.parametrize(
    ("lower", "higher"),
    [("0.000000", "0.000001"), ("0.400000", "0.550000"), ("0.790000", "0.800000"), ("0.950000", "1.000000")],
)
def test_squared_quality_property_is_exact_and_strict(lower, higher):
    low = quality_weight(Decimal(lower))
    high = quality_weight(Decimal(higher))
    assert low >= 0
    assert low < high
    assert high == Decimal(higher) * Decimal(higher)


def test_100_report_50_operator_stress_is_top_k_bounded_and_order_independent():
    first, shuffled = _large_top_k_stress()
    assert first == shuffled
    assert first.eligible_report_count == 100
    assert first.distinct_eligible_operator_count == 50
    assert first.rewarded_operator_count == TaskOperatorRewardConfig().duplicates.top_k == 5
    assert first.distributed_points == first.cluster_reward_points
    assert len({item.operator_id for item in first.operator_allocations if item.rewarded}) == 5
    assert first.chief_operator_id is None or first.chief_operator_id in {
        item.operator_id for item in first.operator_allocations if item.rewarded
    }


@pytest.mark.parametrize("pool", ["0.000001", "0.000007", "1.000001", "10.123457", "9999.999999"])
def test_largest_remainder_rounding_stress_conserves_exact_decimal_pool(pool):
    amount = Decimal(pool)
    items = [
        WeightedRewardItem(
            allocation_id=f"allocation_{index}",
            node_id=f"node_{index}",
            submission_id=f"submission_{index}",
            raw_weight=Decimal(weight),
            contribution_score=Decimal("0"),
        )
        for index, weight in enumerate(("1", "3", "7", "11"), start=1)
    ]
    first = allocate_proportional_rewards(amount, items)
    second = allocate_proportional_rewards(amount, list(reversed(items)))
    assert first == second
    assert sum(first.values()) == amount
    assert all(value >= 0 and value == value.quantize(Decimal("0.000001")) for value in first.values())


def test_public_request_schemas_forbid_quality_chief_and_reward_spoofing():
    with pytest.raises(ValidationError):
        ReportQualityAssessmentRequest.model_validate(
            {
                "correctness_score": "1.000000",
                "poc_quality_score": "1.000000",
                "root_cause_quality_score": "1.000000",
                "impact_quality_score": "1.000000",
                "fix_quality_score": "1.000000",
                "quality_score": "1.000000",
            }
        )
    for spoofed in (
        {"chief_finder": True},
        {"chief_operator_id": "operator_01"},
        {"total_reward_points": "999999.000000"},
    ):
        with pytest.raises(ValidationError):
            Week7TaskRewardCycleCreateRequest.model_validate(
                {"task_reward_budget_id": "budget_1", **spoofed}
            )


def test_machine_readable_outputs_are_deterministically_sorted(tmp_path):
    _, _, outputs = run_week7_reward_benchmark(tmp_path / "results")
    events = json.loads((tmp_path / "results" / "week7_reward_events.json").read_text())
    assert events == outputs["events"]
    assert events == sorted(
        events,
        key=lambda item: (
            item["cluster_id"], item["quality_rank"], item["operator_id"], item["event_id"]
        ),
    )
