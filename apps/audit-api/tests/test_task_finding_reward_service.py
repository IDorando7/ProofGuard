import json
from decimal import Decimal

import pytest

from app.schemas.task_finding_reward import (
    FindingAllocationScope,
    SeverityRewardWeightConfig,
    TaskFindingCalculationProcessingStatus,
    TaskFindingCalculationStatus,
    TaskFindingRewardCalculationRequest,
    TaskFindingRewardConfig,
    UniquenessRewardConfig,
)
from app.schemas.task_reward import TaskRewardBudgetCreateRequest
from app.services.finding_cluster_service import (
    finalize_finding_clusters_for_task,
    rebuild_finding_clusters_for_task,
)
from app.services.task_finding_reward_service import (
    TaskFindingRewardLinkageError,
    TaskFindingRewardNotFoundError,
    TaskFindingRewardStateError,
    calculate_task_finding_rewards,
    get_task_finding_calculation_path,
    list_task_finding_calculations,
    load_latest_task_finding_calculation,
    load_task_finding_calculation,
)
from app.services.task_reward_budget_service import (
    create_task_reward_budget,
    finalize_task_reward_budget,
)
from tests.test_subnet_reward_allocation_service import _setup


def _ready(tmp_path, *, finalize_budget=True, finalize_clusters=True):
    root, workspace, _, routing, _ = _setup(tmp_path)
    result = rebuild_finding_clusters_for_task(
        root, workspace, "project-1", routing.routing_id
    )
    if finalize_clusters:
        clusters = finalize_finding_clusters_for_task(
            root, "project-1", routing.routing_id
        )
    else:
        clusters = result.clusters
    budget, _ = create_task_reward_budget(
        root,
        workspace,
        "project-1",
        TaskRewardBudgetCreateRequest(
            routing_id=routing.routing_id,
            total_budget_points="10000.000000",
        ),
    )
    if finalize_budget:
        budget, _ = finalize_task_reward_budget(
            root, "project-1", budget.task_reward_budget_id
        )
    request = TaskFindingRewardCalculationRequest(
        task_reward_budget_id=budget.task_reward_budget_id
    )
    return root, workspace, routing, clusters, budget, request


def _non_calculation_snapshot(root):
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and "calculations" not in path.parts
    }


def test_category_isolated_default_values_one_cluster_and_conserves_miner_pool(tmp_path):
    root, _, routing, clusters, budget, request = _ready(tmp_path)
    before = _non_calculation_snapshot(root)
    record, status = calculate_task_finding_rewards(
        root, "project-1", routing.routing_id, request
    )
    assert status == TaskFindingCalculationProcessingStatus.CALCULATED
    assert record.allocation_scope == FindingAllocationScope.CATEGORY_ISOLATED
    assert record.miner_pool_points == Decimal("7000.000000")
    assert record.validator_pool_points_snapshot == Decimal("2000.000000")
    assert record.protocol_pool_points_snapshot == Decimal("1000.000000")
    assert record.distributed_cluster_points == Decimal("7000.000000")
    assert record.undistributed_cluster_points == Decimal("0.000000")
    assert record.distributed_cluster_points + record.undistributed_cluster_points == budget.miner_pool_points
    allocation = record.cluster_allocations[0]
    assert allocation.finding_cluster_id == clusters[0].finding_cluster_id
    assert allocation.final_severity.value == "High"
    assert allocation.severity_weight == Decimal("8.000000")
    assert allocation.distinct_operator_count == 1
    assert allocation.uniqueness == Decimal("1.000000")
    assert allocation.finding_score == Decimal("8.000000")
    assert allocation.cluster_reward_points == Decimal("7000.000000")
    assert _non_calculation_snapshot(root) == before
    assert not (root / "rewards" / "events").exists()
    assert not (root / "subnet-rewards" / "events").exists()


def test_global_mode_is_supported_and_quality_data_is_absent(tmp_path):
    root, _, routing, _, budget, _ = _ready(tmp_path)
    request = TaskFindingRewardCalculationRequest(
        task_reward_budget_id=budget.task_reward_budget_id,
        allocation_scope="global",
    )
    record, _ = calculate_task_finding_rewards(
        root, "project-1", routing.routing_id, request
    )
    assert record.category_pool_allocations == []
    assert record.category_weights == {}
    assert record.cluster_allocations[0].parent_pool_type.value == "miner_pool"
    serialized = record.model_dump(mode="json")
    text = json.dumps(serialized)
    for excluded in (
        "quality_score",
        "report_quality",
        "top_k",
        "chief_finder",
        "operator_reward",
        "reputation",
        "membership",
    ):
        assert excluded not in text.lower()


def test_identical_source_is_idempotent_readable_and_deterministic_json(tmp_path):
    root, _, routing, _, _, request = _ready(tmp_path)
    first, _ = calculate_task_finding_rewards(
        root, "project-1", routing.routing_id, request
    )
    path = get_task_finding_calculation_path(
        root, routing.routing_id, first.calculation_id
    )
    bytes_before = path.read_bytes()
    second, status = calculate_task_finding_rewards(
        root, "project-1", routing.routing_id, request
    )
    assert status == TaskFindingCalculationProcessingStatus.UNCHANGED
    assert second == first
    assert path.read_bytes() == bytes_before
    assert load_task_finding_calculation(
        root, "project-1", routing.routing_id, first.calculation_id
    ) == first
    assert load_latest_task_finding_calculation(
        root, "project-1", routing.routing_id
    ) == first
    assert list_task_finding_calculations(
        root, "project-1", routing.routing_id
    ).total == 1
    text = path.read_text(encoding="utf-8")
    assert '"cluster_reward_points": "7000.000000"' in text
    assert "/home/" not in text
    assert "private_key" not in text


def test_policy_change_creates_superseding_immutable_snapshot(tmp_path):
    root, _, routing, _, _, request = _ready(tmp_path)
    first, _ = calculate_task_finding_rewards(
        root, "project-1", routing.routing_id, request
    )
    changed = TaskFindingRewardConfig(
        severity_weights=SeverityRewardWeightConfig(high="9"),
        uniqueness=UniquenessRewardConfig(),
    )
    second, status = calculate_task_finding_rewards(
        root,
        "project-1",
        routing.routing_id,
        request,
        configured_policy=changed,
        configuration_version="changed-config",
    )
    assert status == TaskFindingCalculationProcessingStatus.SUPERSEDED
    assert second.calculation_id != first.calculation_id
    assert second.source_fingerprint != first.source_fingerprint
    history = list_task_finding_calculations(
        root, "project-1", routing.routing_id
    ).calculations
    old = next(item for item in history if item.calculation_id == first.calculation_id)
    assert old.status == TaskFindingCalculationStatus.SUPERSEDED
    assert old.superseded_by_calculation_id == second.calculation_id
    assert second.supersedes_calculation_id == first.calculation_id


def test_quality_reputation_membership_and_timestamps_do_not_enter_fingerprint(tmp_path):
    root, _, routing, _, _, request = _ready(tmp_path)
    first, _ = calculate_task_finding_rewards(
        root, "project-1", routing.routing_id, request
    )
    unrelated = {
        root / "report-quality" / "irrelevant.json": '{"quality_score":"1.000000"}',
        root / "reputation" / "irrelevant.json": '{"reputation":"999"}',
        root / "memberships" / "irrelevant.json": '{"tier":"expert"}',
    }
    for path, contents in unrelated.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    second, status = calculate_task_finding_rewards(
        root, "project-1", routing.routing_id, request
    )
    assert status == TaskFindingCalculationProcessingStatus.UNCHANGED
    assert second.source_fingerprint == first.source_fingerprint
    assert second.calculated_at == first.calculated_at


def test_requires_finalized_budget_and_clusters(tmp_path):
    root, _, routing, _, _, request = _ready(tmp_path, finalize_budget=False)
    with pytest.raises(TaskFindingRewardStateError, match="finalized TaskRewardBudget"):
        calculate_task_finding_rewards(root, "project-1", routing.routing_id, request)

    root, _, routing, _, _, request = _ready(
        tmp_path / "open-cluster", finalize_clusters=False
    )
    with pytest.raises(TaskFindingRewardStateError, match="finalized FindingClusters"):
        calculate_task_finding_rewards(root, "project-1", routing.routing_id, request)


def test_no_clusters_keeps_complete_miner_pool_undistributed(tmp_path):
    root, workspace, _, routing, _ = _setup(tmp_path)
    budget, _ = create_task_reward_budget(
        root,
        workspace,
        "project-1",
        TaskRewardBudgetCreateRequest(
            routing_id=routing.routing_id,
            total_budget_points="10000.000000",
        ),
    )
    budget, _ = finalize_task_reward_budget(
        root, "project-1", budget.task_reward_budget_id
    )
    calculation, _ = calculate_task_finding_rewards(
        root,
        "project-1",
        routing.routing_id,
        TaskFindingRewardCalculationRequest(
            task_reward_budget_id=budget.task_reward_budget_id
        ),
    )
    assert calculation.outcome.value == "no_eligible_findings"
    assert calculation.cluster_allocations == []
    assert calculation.distributed_cluster_points == Decimal("0.000000")
    assert calculation.undistributed_cluster_points == Decimal("7000.000000")
    assert calculation.category_pool_allocations[0].undistributed_cluster_points == Decimal(
        "7000.000000"
    )

def test_missing_and_linkage_errors_are_clear(tmp_path):
    root, _, routing, _, budget, request = _ready(tmp_path)
    with pytest.raises(TaskFindingRewardNotFoundError):
        calculate_task_finding_rewards(root, "project-1", "missing-routing", request)
    wrong = request.model_copy(update={"task_reward_budget_id": "task_reward_budget_wrong"})
    with pytest.raises(TaskFindingRewardLinkageError):
        calculate_task_finding_rewards(root, "project-1", routing.routing_id, wrong)
    assert budget.status.value == "finalized"


def test_client_cannot_supply_server_derived_economic_fields():
    forbidden = (
        "final_severity",
        "distinct_operator_count",
        "uniqueness",
        "finding_score",
        "cluster_reward_points",
        "quality_score",
        "reward_rank",
    )
    for field in forbidden:
        with pytest.raises(Exception):
            TaskFindingRewardCalculationRequest.model_validate(
                {"task_reward_budget_id": "budget-1", field: "1"}
            )
