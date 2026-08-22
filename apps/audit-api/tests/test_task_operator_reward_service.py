import json
from decimal import Decimal

import pytest

from app.schemas.report_quality import ReportQualityAssessmentRequest
from app.schemas.task_finding_reward import TaskFindingRewardCalculationRequest
from app.schemas.task_operator_reward import (
    ChiefFinderConfig,
    DuplicateRewardConfig,
    TaskOperatorProcessingStatus,
    TaskOperatorRewardCalculationRequest,
    TaskOperatorRewardConfig,
)
from app.schemas.task_reward import TaskRewardBudgetCreateRequest
from app.services.finding_cluster_service import (
    finalize_finding_clusters_for_task,
    rebuild_finding_clusters_for_task,
)
from app.services.node_registry_service import load_node, save_node
from app.services.report_quality_assessment_service import assess_report_quality
from app.services.submission_service import load_submission, save_submission
from app.services.task_finding_reward_service import calculate_task_finding_rewards
from app.services.task_operator_reward_service import (
    TaskOperatorRewardLinkageError,
    TaskOperatorRewardNotFoundError,
    calculate_task_operator_rewards,
    get_task_operator_calculation_path,
    list_task_operator_calculations,
    load_latest_task_operator_calculation,
    load_task_operator_calculation,
)
from app.services.task_reward_budget_service import (
    create_task_reward_budget,
    finalize_task_reward_budget,
)
from tests.test_finding_cluster_service import _mark_independent
from tests.test_subnet_reward_allocation_service import _setup
from tests.test_task_finding_reward_service import _ready


def _quality(quality_profile="high"):
    values = {
        "high": ("1.000000", "0.900000", "0.900000", "0.900000", "0.900000"),
        "medium": ("1.000000", "0.700000", "0.700000", "0.700000", "0.700000"),
        "low": ("1.000000", "0.100000", "0.100000", "0.100000", "0.100000"),
    }[quality_profile]
    return ReportQualityAssessmentRequest(
        correctness_score=values[0],
        poc_quality_score=values[1],
        root_cause_quality_score=values[2],
        impact_quality_score=values[3],
        fix_quality_score=values[4],
        reason_codes={
            "impact_quality": [
                "accepted_severity_consistent",
                "validator_confirmed_impact_valid",
            ]
        },
    )


def _single_ready(tmp_path, *, assess=True, draft=False):
    root, workspace, routing, clusters, budget, day4_request = _ready(tmp_path)
    day4, _ = calculate_task_finding_rewards(
        root, "project-1", routing.routing_id, day4_request
    )
    if assess:
        request = None if draft else _quality("high")
        assess_report_quality(
            root,
            workspace,
            "project-1",
            routing.routing_id,
            clusters[0].finding_cluster_id,
            clusters[0].members[0].submission_id,
            request,
        )
    return root, workspace, routing, clusters, budget, day4


def _snapshot_excluding_preview(root):
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and "operator-allocation" not in path.parts
    }


def test_single_finalized_quality_calculates_persists_reads_and_is_idempotent(tmp_path):
    root, _, routing, clusters, budget, day4 = _single_ready(tmp_path)
    before = _snapshot_excluding_preview(root)
    request = TaskOperatorRewardCalculationRequest(
        task_finding_reward_calculation_id=day4.calculation_id
    )
    first, status = calculate_task_operator_rewards(
        root, "project-1", routing.routing_id, request
    )
    assert status == TaskOperatorProcessingStatus.CALCULATED
    payout = first.cluster_payouts[0]
    assert payout.finding_cluster_id == clusters[0].finding_cluster_id
    assert payout.cluster_reward_points == day4.cluster_allocations[0].cluster_reward_points
    assert payout.distributed_points == payout.cluster_reward_points
    assert payout.undistributed_points == 0
    assert payout.rewarded_operator_count == 1
    assert payout.operator_allocations[0].chief_finder
    assert first.distributed_operator_points == Decimal("7000.000000")
    assert first.undistributed_cluster_points == 0
    assert first.operator_summaries[0].total_reward_points == Decimal("7000.000000")
    assert _snapshot_excluding_preview(root) == before
    assert budget.status.value == "finalized"
    assert not (root / "rewards" / "events").exists()

    path = get_task_operator_calculation_path(
        root, routing.routing_id, first.calculation_id
    )
    bytes_before = path.read_bytes()
    repeated, status = calculate_task_operator_rewards(
        root, "project-1", routing.routing_id, request
    )
    assert status == TaskOperatorProcessingStatus.UNCHANGED
    assert repeated == first
    assert path.read_bytes() == bytes_before
    assert load_task_operator_calculation(
        root, "project-1", routing.routing_id, first.calculation_id
    ) == first
    assert load_latest_task_operator_calculation(
        root, "project-1", routing.routing_id
    ) == first
    assert list_task_operator_calculations(
        root, "project-1", routing.routing_id
    ).total == 1
    text = path.read_text(encoding="utf-8")
    assert '"quality_weight":' in text
    assert "/home/" not in text
    assert "private_key" not in text


@pytest.mark.parametrize(("assess", "draft", "reason"), [(False, False, "missing_quality_assessment"), (True, True, "draft_quality_assessment")])
def test_missing_or_draft_quality_leaves_cluster_reward_undistributed(
    tmp_path, assess, draft, reason
):
    root, _, routing, _, _, day4 = _single_ready(
        tmp_path, assess=assess, draft=draft
    )
    calculation, _ = calculate_task_operator_rewards(
        root,
        "project-1",
        routing.routing_id,
        TaskOperatorRewardCalculationRequest(
            task_finding_reward_calculation_id=day4.calculation_id
        ),
    )
    payout = calculation.cluster_payouts[0]
    assert payout.distributed_points == 0
    assert payout.undistributed_points == payout.cluster_reward_points
    assert payout.report_exclusions[0].reason.value == reason


@pytest.mark.parametrize(
    "impact_reason_codes",
    [
        ["accepted_severity_consistent"],
        ["validator_confirmed_impact_valid"],
    ],
)
def test_chief_requires_separate_severity_and_impact_confirmation(
    tmp_path, impact_reason_codes
):
    root, workspace, routing, clusters, _, day4 = _single_ready(
        tmp_path, assess=False
    )
    values = _quality("high").model_dump()
    values["reason_codes"] = {"impact_quality": impact_reason_codes}
    assess_report_quality(
        root,
        workspace,
        "project-1",
        routing.routing_id,
        clusters[0].finding_cluster_id,
        clusters[0].members[0].submission_id,
        ReportQualityAssessmentRequest.model_validate(values),
    )
    calculation, _ = calculate_task_operator_rewards(
        root,
        "project-1",
        routing.routing_id,
        TaskOperatorRewardCalculationRequest(
            task_finding_reward_calculation_id=day4.calculation_id
        ),
    )
    payout = calculation.cluster_payouts[0]
    assert payout.chief_operator_id is None
    assert payout.chief_bonus_pool_points == 0
    assert payout.quality_pool_points == payout.cluster_reward_points


def test_two_nodes_same_operator_create_one_position_and_best_q_wins(tmp_path):
    root, workspace, _, routing, submissions = _setup(tmp_path, include_candidate=True)
    first_submission = submissions[0][0]
    second_submission = submissions[1][0]
    _mark_independent(workspace, second_submission.finding_id, first_submission.finding_id)
    first_node = load_node(root, first_submission.node_id)
    second_node = load_node(root, second_submission.node_id)
    save_node(root, second_node.model_copy(update={"operator_id": first_node.operator_id}))
    cluster = rebuild_finding_clusters_for_task(
        root, workspace, "project-1", routing.routing_id
    ).clusters[0]
    cluster = finalize_finding_clusters_for_task(
        root, "project-1", routing.routing_id
    )[0]
    budget, _ = create_task_reward_budget(
        root,
        workspace,
        "project-1",
        TaskRewardBudgetCreateRequest(
            routing_id=routing.routing_id, total_budget_points="10000.000000"
        ),
    )
    budget, _ = finalize_task_reward_budget(
        root, "project-1", budget.task_reward_budget_id
    )
    day4, _ = calculate_task_finding_rewards(
        root,
        "project-1",
        routing.routing_id,
        TaskFindingRewardCalculationRequest(
            task_reward_budget_id=budget.task_reward_budget_id
        ),
    )
    for submission, quality in (
        (first_submission, _quality("medium")),
        (second_submission, _quality("high")),
    ):
        assess_report_quality(
            root,
            workspace,
            "project-1",
            routing.routing_id,
            cluster.finding_cluster_id,
            submission.submission_id,
            quality,
        )
    calculation, _ = calculate_task_operator_rewards(
        root,
        "project-1",
        routing.routing_id,
        TaskOperatorRewardCalculationRequest(
            task_finding_reward_calculation_id=day4.calculation_id
        ),
    )
    payout = calculation.cluster_payouts[0]
    assert payout.distinct_eligible_operator_count == 1
    assert len(payout.operator_allocations) == 1
    assert payout.operator_allocations[0].rewarded_submission_id == second_submission.submission_id
    assert any(
        item.submission_id == first_submission.submission_id
        and item.reason.value == "superseded_by_operator_best"
        for item in payout.report_exclusions
    )


def test_authorized_candidate_shadow_and_active_reports_use_q_only_without_tier_multiplier(tmp_path):
    root, workspace, _, routing, submissions = _setup(tmp_path, include_candidate=True)
    first_submission = submissions[0][0]
    second_submission = submissions[1][0]
    _mark_independent(workspace, second_submission.finding_id, first_submission.finding_id)
    cluster = rebuild_finding_clusters_for_task(
        root, workspace, "project-1", routing.routing_id
    ).clusters[0]
    finalize_finding_clusters_for_task(root, "project-1", routing.routing_id)
    budget, _ = create_task_reward_budget(
        root,
        workspace,
        "project-1",
        TaskRewardBudgetCreateRequest(
            routing_id=routing.routing_id, total_budget_points="10000.000000"
        ),
    )
    budget, _ = finalize_task_reward_budget(root, "project-1", budget.task_reward_budget_id)
    day4, _ = calculate_task_finding_rewards(
        root,
        "project-1",
        routing.routing_id,
        TaskFindingRewardCalculationRequest(task_reward_budget_id=budget.task_reward_budget_id),
    )
    for submission in (first_submission, second_submission):
        assess_report_quality(
            root,
            workspace,
            "project-1",
            routing.routing_id,
            cluster.finding_cluster_id,
            submission.submission_id,
            _quality("high"),
        )
    calculation, _ = calculate_task_operator_rewards(
        root,
        "project-1",
        routing.routing_id,
        TaskOperatorRewardCalculationRequest(
            task_finding_reward_calculation_id=day4.calculation_id
        ),
    )
    allocations = calculation.cluster_payouts[0].operator_allocations
    assert len(allocations) == 2
    assert allocations[0].quality_weight == allocations[1].quality_weight
    assert allocations[0].quality_reward_points == allocations[1].quality_reward_points
    modes = {
        assignment.node_id: assignment.assignment_mode.value
        for assignment in routing.results[0].assignments
    }
    assert set(modes.values()) == {"production", "shadow"}


def test_policy_changes_supersede_and_excluded_performance_data_does_not_change_fingerprint(tmp_path):
    root, _, routing, _, _, day4 = _single_ready(tmp_path)
    request = TaskOperatorRewardCalculationRequest(
        task_finding_reward_calculation_id=day4.calculation_id
    )
    first, _ = calculate_task_operator_rewards(
        root, "project-1", routing.routing_id, request
    )
    for path, payload in {
        root / "reputation" / "noise.json": {"score": "0.99"},
        root / "category-scores" / "noise.json": {"score": "1"},
        root / "memberships" / "noise.json": {"tier": "expert"},
        root / "contributions" / "noise.json": {"score": "100"},
    }.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    unchanged, status = calculate_task_operator_rewards(
        root, "project-1", routing.routing_id, request
    )
    assert status == TaskOperatorProcessingStatus.UNCHANGED
    assert unchanged.source_fingerprint == first.source_fingerprint

    changed, status = calculate_task_operator_rewards(
        root,
        "project-1",
        routing.routing_id,
        request,
        configured_policy=TaskOperatorRewardConfig(
            duplicates=DuplicateRewardConfig(top_k=1),
            chief_finder=ChiefFinderConfig(
                bonus_percentage="0.10",
                quality_percentage="0.90",
                quality_threshold="0.90",
            ),
        ),
        configuration_version="changed-config",
    )
    assert status == TaskOperatorProcessingStatus.SUPERSEDED
    assert changed.source_fingerprint != first.source_fingerprint
    history = list_task_operator_calculations(
        root, "project-1", routing.routing_id
    ).calculations
    old = next(item for item in history if item.calculation_id == first.calculation_id)
    assert old.status.value == "superseded"
    assert old.superseded_by_calculation_id == changed.calculation_id


def test_unauthorized_assignment_is_excluded_without_moving_cluster_reward(tmp_path):
    root, _, routing, clusters, _, day4 = _single_ready(tmp_path)
    member = clusters[0].members[0]
    submission = load_submission(root, member.submission_id)
    save_submission(
        root,
        submission.model_copy(update={"routing_assignment_id": "assignment-forged"}),
    )
    calculation, _ = calculate_task_operator_rewards(
        root,
        "project-1",
        routing.routing_id,
        TaskOperatorRewardCalculationRequest(
            task_finding_reward_calculation_id=day4.calculation_id
        ),
    )
    payout = calculation.cluster_payouts[0]
    assert payout.distributed_points == 0
    assert payout.undistributed_points == day4.cluster_allocations[0].cluster_reward_points
    assert payout.report_exclusions[0].reason.value == "unauthorized_assignment"
    assert day4.cluster_allocations[0].cluster_reward_points == Decimal("7000.000000")


def test_operator_ownership_change_changes_preview_fingerprint_and_never_spoofs_slot(tmp_path):
    root, _, routing, clusters, _, day4 = _single_ready(tmp_path)
    request = TaskOperatorRewardCalculationRequest(
        task_finding_reward_calculation_id=day4.calculation_id
    )
    first, _ = calculate_task_operator_rewards(
        root, "project-1", routing.routing_id, request
    )
    node = load_node(root, clusters[0].members[0].node_id)
    save_node(root, node.model_copy(update={"operator_id": "different-operator"}))
    changed, status = calculate_task_operator_rewards(
        root, "project-1", routing.routing_id, request
    )
    assert status == TaskOperatorProcessingStatus.SUPERSEDED
    assert changed.source_fingerprint != first.source_fingerprint
    payout = changed.cluster_payouts[0]
    assert payout.operator_allocations == []
    assert payout.report_exclusions[0].reason.value == "operator_identity_mismatch"
    assert payout.undistributed_points == payout.cluster_reward_points


def test_missing_day4_and_wrong_project_linkage_fail(tmp_path):
    root, _, routing, _, _, day4 = _single_ready(tmp_path)
    with pytest.raises(TaskOperatorRewardNotFoundError):
        calculate_task_operator_rewards(
            root,
            "project-1",
            routing.routing_id,
            TaskOperatorRewardCalculationRequest(
                task_finding_reward_calculation_id="missing-calculation"
            ),
        )
    with pytest.raises((TaskOperatorRewardLinkageError, TaskOperatorRewardNotFoundError)):
        calculate_task_operator_rewards(
            root,
            "project-2",
            routing.routing_id,
            TaskOperatorRewardCalculationRequest(
                task_finding_reward_calculation_id=day4.calculation_id
            ),
        )


def test_client_request_forbids_all_derived_fields():
    forbidden = (
        "quality_score",
        "operator_id",
        "top_k_operator_ids",
        "quality_rank",
        "chief_operator_id",
        "chief_submission_id",
        "quality_weight",
        "total_reward_points",
        "rewarded",
    )
    for field in forbidden:
        with pytest.raises(Exception):
            TaskOperatorRewardCalculationRequest.model_validate(
                {
                    "task_finding_reward_calculation_id": "day4-calculation",
                    field: "forged",
                }
            )
