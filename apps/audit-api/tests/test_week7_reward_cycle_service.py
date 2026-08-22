import json
import shutil
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.reward import RewardCycleStatus
from app.schemas.report_quality import ReportQualityConfig
from app.schemas.task_finding_reward import FindingAllocationScope
from app.schemas.task_finding_reward import (
    SeverityRewardWeightConfig,
    TaskFindingRewardConfig,
    UniquenessRewardConfig,
)
from app.schemas.task_operator_reward import (
    ChiefFinderConfig,
    DuplicateRewardConfig,
    TaskOperatorRewardConfig,
)
from app.schemas.week7_reward_cycle import (
    Week7RewardHistorySubject,
    Week7RewardProcessingStatus,
    Week7RewardVerificationStatus,
    Week7TaskRewardCycle,
    Week7TaskRewardCycleCreateRequest,
)
from app.services.node_registry_service import load_node, save_node
from app.services.finding_cluster_service import get_cluster_path
from app.services.report_quality_assessment_service import (
    get_report_quality_assessment_path,
    load_active_report_quality_assessment,
)
from app.services.subnet_router_service import get_routing_record_path
from app.services.task_reward_budget_service import get_task_reward_budget_path
from app.services.task_operator_reward_service import get_task_operator_calculation_path
from app.services.week7_reward_cycle_service import (
    Week7RewardCycleConservationError,
    Week7RewardCycleConflictError,
    Week7RewardCycleSourceChangedError,
    Week7RewardCycleStateError,
    Week7RewardEventConflictError,
    calculate_week7_task_reward_cycle,
    create_week7_task_reward_cycle,
    finalize_week7_task_reward_cycle,
    get_week7_reward_event_path,
    get_week7_reward_history,
    list_project_week7_reward_cycles,
    list_week7_reward_events,
    load_week7_reward_cycle,
    save_week7_reward_cycle,
    verify_week7_task_reward_cycle,
)
from tests.test_task_operator_reward_service import _single_ready


def _draft_cycle(tmp_path):
    root, workspace, routing, clusters, budget, _ = _single_ready(tmp_path)
    cycle, created = create_week7_task_reward_cycle(
        root,
        workspace,
        "project-1",
        routing.routing_id,
        Week7TaskRewardCycleCreateRequest(
            task_reward_budget_id=budget.task_reward_budget_id
        ),
    )
    assert created
    return root, workspace, routing, clusters, budget, cycle


def _calculated_cycle(tmp_path):
    root, workspace, routing, clusters, budget, cycle = _draft_cycle(tmp_path)
    cycle, status = calculate_week7_task_reward_cycle(
        root, "project-1", cycle.reward_cycle_id
    )
    assert status == Week7RewardProcessingStatus.CALCULATED
    return root, workspace, routing, clusters, budget, cycle


def test_cycle_schema_draft_calculated_finalized_and_immutable(tmp_path):
    root, _, _, _, _, draft = _draft_cycle(tmp_path)
    assert draft.status == RewardCycleStatus.DRAFT
    assert draft.policy_snapshot is not None
    assert draft.policy_snapshot.fingerprint_version == "reward_cycle_fingerprint_v1"
    assert draft.policy_snapshot.protocol_quantum == Decimal("0.000001")
    assert draft.policy_snapshot.task_operator_reward.duplicates.top_k == 5
    assert draft.distributed_miner_points == 0
    assert draft.undistributed_miner_points == draft.miner_pool_points
    assert draft.reward_event_count == 0
    with pytest.raises((ValidationError, ValueError)):
        Week7TaskRewardCycle.model_validate(
            {**draft.model_dump(), "reward_domain": "network_protocol"}
        )
    missing_policy = draft.model_dump()
    missing_policy.pop("policy_version")
    with pytest.raises((ValidationError, ValueError)):
        Week7TaskRewardCycle.model_validate(missing_policy)
    with pytest.raises((ValidationError, ValueError)):
        Week7TaskRewardCycle.model_validate(
            {**draft.model_dump(), "status": "calculated"}
        )
    with pytest.raises((ValidationError, ValueError)):
        Week7TaskRewardCycle.model_validate(
            {**draft.model_dump(), "status": "finalized"}
        )
    legacy_payload = draft.model_dump()
    legacy_payload.pop("policy_snapshot")
    assert Week7TaskRewardCycle.model_validate(legacy_payload).policy_snapshot is None
    calculated, _ = calculate_week7_task_reward_cycle(
        root, "project-1", draft.reward_cycle_id
    )
    assert calculated.status == RewardCycleStatus.CALCULATED
    assert calculated.calculation_fingerprint
    finalized = finalize_week7_task_reward_cycle(
        root, "project-1", draft.reward_cycle_id
    ).cycle
    assert finalized.status == RewardCycleStatus.FINALIZED
    assert finalized.finalized_at
    assert finalized.finalization_source_fingerprint
    with pytest.raises((ValidationError, TypeError)):
        finalized.status = RewardCycleStatus.DRAFT


def test_create_is_idempotent_and_conflicting_cycle_for_budget_is_rejected(tmp_path):
    root, workspace, routing, _, budget, cycle = _draft_cycle(tmp_path)
    repeated, created = create_week7_task_reward_cycle(
        root,
        workspace,
        "project-1",
        routing.routing_id,
        Week7TaskRewardCycleCreateRequest(
            task_reward_budget_id=budget.task_reward_budget_id
        ),
    )
    assert not created
    assert repeated == cycle
    with pytest.raises(Week7RewardCycleConflictError):
        create_week7_task_reward_cycle(
            root,
            workspace,
            "project-1",
            routing.routing_id,
            Week7TaskRewardCycleCreateRequest(
                task_reward_budget_id=budget.task_reward_budget_id,
                allocation_scope=FindingAllocationScope.GLOBAL,
            ),
        )


def test_calculate_integrates_day4_day5_without_events_and_is_idempotent(tmp_path):
    root, _, _, _, budget, cycle = _calculated_cycle(tmp_path)
    assert cycle.finding_calculation_id
    assert cycle.operator_calculation_id
    assert cycle.calculation_revision == 1
    assert cycle.distributed_miner_points == Decimal("7000.000000")
    assert cycle.undistributed_miner_points == 0
    assert cycle.distributed_miner_points + cycle.undistributed_miner_points == budget.miner_pool_points
    assert cycle.validator_pool_points == budget.validator_pool_points
    assert cycle.protocol_pool_points == budget.protocol_pool_points
    assert list_week7_reward_events(root) == []
    repeated, status = calculate_week7_task_reward_cycle(
        root, "project-1", cycle.reward_cycle_id
    )
    assert status == Week7RewardProcessingStatus.UNCHANGED
    assert repeated == cycle
    assert repeated.calculated_at == cycle.calculated_at
    assert not (root / "rewards" / "events").exists()


def test_calculated_cycle_verification_is_read_only_and_retry_safe(tmp_path):
    root, _, _, _, _, cycle = _calculated_cycle(tmp_path)
    cycle_path = next(root.glob("task-rewards/cycles/routing/*/*/cycle.json"))
    before = cycle_path.read_bytes()

    verification = verify_week7_task_reward_cycle(
        root, "project-1", cycle.reward_cycle_id
    )

    assert verification.verification_status == (
        Week7RewardVerificationStatus.CALCULATED_NOT_FINALIZED
    )
    assert verification.ok
    assert verification.safe_retry_finalize
    assert verification.source_fingerprint_match
    assert verification.calculation_fingerprint_match
    assert verification.budget_conserved
    assert verification.cluster_rewards_conserved
    assert verification.reporter_rewards_conserved
    assert verification.stored_event_count == 0
    assert cycle_path.read_bytes() == before
    assert list_week7_reward_events(root) == []


def test_finalize_materializes_exact_event_and_repeated_finalize_is_idempotent(tmp_path):
    root, _, _, clusters, budget, calculated = _calculated_cycle(tmp_path)
    result = finalize_week7_task_reward_cycle(
        root, "project-1", calculated.reward_cycle_id
    )
    assert result.processing_status == Week7RewardProcessingStatus.FINALIZED
    assert result.reward_events_created == 1
    cycle = result.cycle
    events = list_week7_reward_events(root, reward_cycle_id=cycle.reward_cycle_id)
    assert len(events) == cycle.reward_event_count == 1
    event = events[0]
    assert event.reward_domain.value == "client_task"
    assert event.finding_cluster_id == clusters[0].finding_cluster_id
    assert event.operator_id == clusters[0].members[0].operator_id
    assert event.node_id == clusters[0].members[0].node_id
    assert event.submission_id == clusters[0].members[0].submission_id
    assert event.quality_weight == event.quality_score * event.quality_score
    assert event.chief_finder
    assert event.chief_qualifying_submission_id
    assert event.total_reward_points == Decimal("7000.000000")
    assert event.total_reward_points == event.quality_reward_points + event.chief_bonus_points
    assert sum(item.total_reward_points for item in events) == cycle.distributed_miner_points
    assert cycle.distributed_miner_points + cycle.undistributed_miner_points == budget.miner_pool_points

    repeated = finalize_week7_task_reward_cycle(
        root, "project-1", calculated.reward_cycle_id
    )
    assert repeated.processing_status == Week7RewardProcessingStatus.ALREADY_FINALIZED
    assert repeated.reward_events_created == 0
    assert repeated.reward_events_existing == 1
    assert repeated.cycle == cycle
    assert list_week7_reward_events(root) == events
    cycle_path = next(root.glob("task-rewards/cycles/routing/*/*/cycle.json"))
    event_path = next(root.glob("task-rewards/events/cycles/*/*.json"))
    serialized = cycle_path.read_text(encoding="utf-8") + event_path.read_text(
        encoding="utf-8"
    )
    assert "/home/" not in serialized
    assert "private_key" not in serialized
    with pytest.raises(Week7RewardCycleStateError):
        calculate_week7_task_reward_cycle(root, "project-1", cycle.reward_cycle_id)


def test_finalized_cycle_verifies_complete_immutable_ledger(tmp_path):
    root, _, _, _, _, calculated = _calculated_cycle(tmp_path)
    finalized = finalize_week7_task_reward_cycle(
        root, "project-1", calculated.reward_cycle_id
    ).cycle

    verification = verify_week7_task_reward_cycle(
        root, "project-1", finalized.reward_cycle_id
    )

    assert verification.verification_status == (
        Week7RewardVerificationStatus.CLEAN_FINALIZED
    )
    assert verification.ok
    assert verification.event_set_complete
    assert not verification.duplicate_events_found
    assert verification.expected_event_count == verification.stored_event_count == 1
    assert verification.expected_event_total == verification.stored_event_total
    assert verification.stored_event_total == finalized.distributed_miner_points


def test_verification_classifies_missing_day5_reference_as_corrupt(tmp_path):
    root, _, routing, _, _, calculated = _calculated_cycle(tmp_path)
    path = get_task_operator_calculation_path(
        root, routing.routing_id, calculated.operator_calculation_id
    )
    path.unlink()

    verification = verify_week7_task_reward_cycle(
        root, "project-1", calculated.reward_cycle_id
    )

    assert verification.verification_status == (
        Week7RewardVerificationStatus.CORRUPT_REFERENCES
    )
    assert not verification.ok
    assert not verification.immutable_source_consistent


def test_verification_detects_same_total_event_tampering(tmp_path):
    root, _, _, _, _, calculated = _calculated_cycle(tmp_path)
    finalized = finalize_week7_task_reward_cycle(
        root, "project-1", calculated.reward_cycle_id
    ).cycle
    event = list_week7_reward_events(root, reward_cycle_id=finalized.reward_cycle_id)[0]
    path = get_week7_reward_event_path(root, event.reward_cycle_id, event.reward_event_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["quality_reward_points"] = str(
        Decimal(payload["quality_reward_points"]) - Decimal("1.000000")
    )
    payload["chief_bonus_points"] = str(
        Decimal(payload["chief_bonus_points"]) + Decimal("1.000000")
    )
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    verification = verify_week7_task_reward_cycle(
        root, "project-1", finalized.reward_cycle_id
    )

    assert verification.verification_status == (
        Week7RewardVerificationStatus.FINALIZED_EVENT_MISMATCH
    )
    assert not verification.ok
    assert not verification.event_set_complete
    assert any("conflicts" in error for error in verification.errors)
    with pytest.raises(Week7RewardCycleConservationError):
        finalize_week7_task_reward_cycle(
            root, "project-1", finalized.reward_cycle_id
        )


def test_draft_cannot_finalize(tmp_path):
    root, _, _, _, _, draft = _draft_cycle(tmp_path)
    with pytest.raises(Week7RewardCycleStateError):
        finalize_week7_task_reward_cycle(root, "project-1", draft.reward_cycle_id)
    assert list_week7_reward_events(root) == []


def test_missing_finalized_quality_can_finalize_as_explicitly_undistributed(tmp_path):
    root, workspace, routing, _, budget, _ = _single_ready(
        tmp_path, assess=False
    )
    draft, _ = create_week7_task_reward_cycle(
        root,
        workspace,
        "project-1",
        routing.routing_id,
        Week7TaskRewardCycleCreateRequest(
            task_reward_budget_id=budget.task_reward_budget_id
        ),
    )
    calculated, _ = calculate_week7_task_reward_cycle(
        root, "project-1", draft.reward_cycle_id
    )
    assert calculated.distributed_miner_points == 0
    assert calculated.undistributed_miner_points == budget.miner_pool_points
    finalized = finalize_week7_task_reward_cycle(
        root, "project-1", draft.reward_cycle_id
    ).cycle
    assert finalized.status == RewardCycleStatus.FINALIZED
    assert finalized.reward_event_count == 0
    assert finalized.undistributed_miner_points == budget.miner_pool_points
    assert list_week7_reward_events(root) == []


def test_authoritative_operator_change_blocks_finalization_without_events(tmp_path):
    root, _, _, clusters, _, calculated = _calculated_cycle(tmp_path)
    node = load_node(root, clusters[0].members[0].node_id)
    save_node(root, node.model_copy(update={"operator_id": "changed-operator"}))
    with pytest.raises(Week7RewardCycleSourceChangedError):
        finalize_week7_task_reward_cycle(
            root, "project-1", calculated.reward_cycle_id
        )
    assert list_week7_reward_events(root) == []
    stored = load_week7_reward_cycle(root, "project-1", calculated.reward_cycle_id)
    assert stored.status == RewardCycleStatus.CALCULATED


def test_non_economic_node_display_change_does_not_stale_cycle(tmp_path):
    root, _, _, clusters, _, calculated = _calculated_cycle(tmp_path)
    node = load_node(root, clusters[0].members[0].node_id)
    save_node(
        root,
        node.model_copy(update={"display_name": "Renamed Economic-Neutral Node"}),
    )
    finalized = finalize_week7_task_reward_cycle(
        root, "project-1", calculated.reward_cycle_id
    ).cycle
    assert finalized.status == RewardCycleStatus.FINALIZED
    assert finalized.calculation_fingerprint == calculated.calculation_fingerprint


@pytest.mark.parametrize("source_kind", ["budget", "routing", "cluster", "quality"])
def test_economic_source_fingerprint_changes_block_finalization(
    tmp_path, source_kind
):
    root, _, routing, clusters, _, calculated = _calculated_cycle(tmp_path)
    member = clusters[0].members[0]
    if source_kind == "budget":
        path = get_task_reward_budget_path(root, routing.routing_id)
    elif source_kind == "routing":
        path = get_routing_record_path(root, "project-1", routing.routing_id)
    elif source_kind == "cluster":
        path = get_cluster_path(
            root, routing.routing_id, clusters[0].finding_cluster_id
        )
    else:
        assessment = load_active_report_quality_assessment(
            root,
            routing.routing_id,
            member.submission_id,
            "report_quality_policy_v1",
        )
        path = get_report_quality_assessment_path(
            root,
            routing.routing_id,
            member.submission_id,
            assessment.assessment_version,
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["source_fingerprint"] = "f" * 64
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    with pytest.raises((Week7RewardCycleSourceChangedError, ValueError)):
        finalize_week7_task_reward_cycle(
            root, "project-1", calculated.reward_cycle_id
        )
    assert list_week7_reward_events(root) == []


@pytest.mark.parametrize(
    "setting_name,replacement",
    [
        (
            "task_operator_reward",
            TaskOperatorRewardConfig(
                duplicates=DuplicateRewardConfig(top_k=1),
                chief_finder=ChiefFinderConfig(
                    bonus_percentage="0.05",
                    quality_percentage="0.95",
                    quality_threshold="0.80",
                ),
            ),
        ),
        (
            "task_operator_reward",
            TaskOperatorRewardConfig(
                duplicates=DuplicateRewardConfig(top_k=5),
                chief_finder=ChiefFinderConfig(
                    bonus_percentage="0.10",
                    quality_percentage="0.90",
                    quality_threshold="0.90",
                ),
            ),
        ),
        (
            "task_finding_reward",
            TaskFindingRewardConfig(
                severity_weights=SeverityRewardWeightConfig(high="9"),
                uniqueness=UniquenessRewardConfig(),
            ),
        ),
        (
            "task_finding_reward",
            TaskFindingRewardConfig(
                severity_weights=SeverityRewardWeightConfig(),
                uniqueness=UniquenessRewardConfig(
                    coefficient="0.25", floor="0.50"
                ),
            ),
        ),
        (
            "report_quality",
            ReportQualityConfig(
                correctness="0.40",
                poc_quality="0.20",
                root_cause="0.20",
                impact="0.10",
                fix="0.10",
            ),
        ),
    ],
)
def test_economic_configuration_changes_block_finalization(
    tmp_path, monkeypatch, setting_name, replacement
):
    root, _, _, _, _, calculated = _calculated_cycle(tmp_path)
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), setting_name, replacement)
    with pytest.raises(Week7RewardCycleSourceChangedError):
        finalize_week7_task_reward_cycle(
            root, "project-1", calculated.reward_cycle_id
        )
    assert list_week7_reward_events(root) == []


def test_partial_crash_retry_reuses_deterministic_event(tmp_path):
    root, _, _, _, _, calculated = _calculated_cycle(tmp_path)
    with pytest.raises(RuntimeError, match="Simulated"):
        finalize_week7_task_reward_cycle(
            root,
            "project-1",
            calculated.reward_cycle_id,
            fail_after_event_writes=1,
        )
    partial = list_week7_reward_events(root)
    assert len(partial) == 1
    stored_cycle = load_week7_reward_cycle(
        root, "project-1", calculated.reward_cycle_id
    )
    assert stored_cycle.status == RewardCycleStatus.CALCULATED
    verification = verify_week7_task_reward_cycle(
        root, "project-1", calculated.reward_cycle_id
    )
    assert verification.verification_status == (
        Week7RewardVerificationStatus.EVENT_SET_COMPLETE_STATUS_NOT_FINALIZED
    )
    assert verification.safe_retry_finalize
    assert verification.event_set_complete

    completed = finalize_week7_task_reward_cycle(
        root, "project-1", calculated.reward_cycle_id
    )
    assert completed.reward_events_created == 0
    assert completed.reward_events_existing == 1
    assert completed.cycle.status == RewardCycleStatus.FINALIZED
    assert list_week7_reward_events(root)[0].reward_event_id == partial[0].reward_event_id


def test_conflicting_deterministic_event_is_never_overwritten(tmp_path):
    root, _, _, _, _, calculated = _calculated_cycle(tmp_path)
    with pytest.raises(RuntimeError):
        finalize_week7_task_reward_cycle(
            root,
            "project-1",
            calculated.reward_cycle_id,
            fail_after_event_writes=1,
        )
    event = list_week7_reward_events(root)[0]
    path = get_week7_reward_event_path(
        root, event.reward_cycle_id, event.reward_event_id
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["quality_reward_points"] = "6999.000000"
    payload["total_reward_points"] = str(
        Decimal(payload["quality_reward_points"])
        + Decimal(payload["chief_bonus_points"])
    )
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(Week7RewardEventConflictError):
        finalize_week7_task_reward_cycle(
            root, "project-1", calculated.reward_cycle_id
        )
    assert path.read_bytes() == before
    assert load_week7_reward_cycle(
        root, "project-1", calculated.reward_cycle_id
    ).status == RewardCycleStatus.CALCULATED


def test_second_cycle_cannot_finalize_same_task_budget(tmp_path):
    root, _, _, _, _, first = _calculated_cycle(tmp_path)
    second_draft = Week7TaskRewardCycle.model_validate(
        {
            **first.model_dump(),
            "reward_cycle_id": "week7_task_reward_cycle_" + "a" * 32,
            "status": RewardCycleStatus.DRAFT,
            "finding_calculation_id": None,
            "finding_calculation_source_fingerprint": None,
            "operator_calculation_id": None,
            "operator_calculation_source_fingerprint": None,
            "distributed_miner_points": "0.000000",
            "undistributed_miner_points": first.miner_pool_points,
            "rewarded_operator_count": 0,
            "rewarded_cluster_count": 0,
            "chief_finder_count": 0,
            "calculation_fingerprint": None,
            "calculation_revision": 0,
            "superseded_calculation_fingerprints": [],
            "calculated_at": None,
        }
    )
    save_week7_reward_cycle(root, second_draft)
    second, _ = calculate_week7_task_reward_cycle(
        root, "project-1", second_draft.reward_cycle_id
    )
    finalized_first = finalize_week7_task_reward_cycle(
        root, "project-1", first.reward_cycle_id
    ).cycle
    assert finalized_first.status == RewardCycleStatus.FINALIZED
    with pytest.raises(Week7RewardCycleConflictError):
        finalize_week7_task_reward_cycle(
            root, "project-1", second.reward_cycle_id
        )
    assert all(
        event.reward_cycle_id == first.reward_cycle_id
        for event in list_week7_reward_events(root)
    )


def test_economic_histories_are_separate_and_auditable(tmp_path):
    root, _, _, clusters, _, calculated = _calculated_cycle(tmp_path)
    finalized = finalize_week7_task_reward_cycle(
        root, "project-1", calculated.reward_cycle_id
    ).cycle
    event = list_week7_reward_events(root)[0]
    for subject, identifier in (
        (Week7RewardHistorySubject.OPERATOR, event.operator_id),
        (Week7RewardHistorySubject.NODE, event.node_id),
        (Week7RewardHistorySubject.SUBMISSION, event.submission_id),
        (Week7RewardHistorySubject.FINDING_CLUSTER, event.finding_cluster_id),
    ):
        summary = get_week7_reward_history(root, subject, identifier)
        assert summary.total_reward_events == 1
        assert summary.total_client_task_reward_points == finalized.distributed_miner_points
        assert summary.rewarded_cluster_count == 1
        assert summary.chief_finder_count == 1
    assert list_project_week7_reward_cycles(root, "project-1").total == 1
    assert not (root / "reputation-events" / "week7").exists()
    assert clusters[0].members[0].operator_id == event.operator_id


def test_deterministic_replay_across_clean_roots(tmp_path):
    source_root, workspace, routing, _, budget, _ = _single_ready(tmp_path / "source")
    roots = []
    outputs = []
    for name in ("replay-a", "replay-b"):
        root = tmp_path / name / "protocol"
        shutil.copytree(source_root, root)
        cycle, _ = create_week7_task_reward_cycle(
            root,
            workspace,
            "project-1",
            routing.routing_id,
            Week7TaskRewardCycleCreateRequest(
                task_reward_budget_id=budget.task_reward_budget_id
            ),
        )
        calculated, _ = calculate_week7_task_reward_cycle(
            root, "project-1", cycle.reward_cycle_id
        )
        finalized = finalize_week7_task_reward_cycle(
            root, "project-1", cycle.reward_cycle_id
        ).cycle
        events = list_week7_reward_events(root)
        roots.append(root)
        outputs.append(
            {
                "cycle_id": finalized.reward_cycle_id,
                "calculation_fingerprint": finalized.calculation_fingerprint,
                "finalization_fingerprint": finalized.finalization_source_fingerprint,
                "distributed": finalized.distributed_miner_points,
                "undistributed": finalized.undistributed_miner_points,
                "events": [
                    event.model_dump(exclude={"created_at", "finalized_at"})
                    for event in events
                ],
                "day4": calculated.finding_calculation_id,
                "day5": calculated.operator_calculation_id,
            }
        )
    assert outputs[0] == outputs[1]
    assert roots[0] != roots[1]


def test_client_cycle_request_forbids_derived_rewards():
    for field in (
        "distributed_miner_points",
        "reward_event_id",
        "operator_id",
        "quality_score",
        "chief_finder",
        "total_reward_points",
        "status",
    ):
        with pytest.raises(ValidationError):
            Week7TaskRewardCycleCreateRequest.model_validate(
                {"task_reward_budget_id": "budget-1", field: "forged"}
            )
