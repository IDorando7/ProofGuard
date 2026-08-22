from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.reward import RewardCycleStatus, RewardPoolKind
from app.schemas.task_reward import TaskRewardBudgetCreateRequest
from app.schemas.reproduction import ReproductionStatus
from app.schemas.validation import ValidationStatus
from app.schemas.validator_attestation import (
    ImpactDecision,
    RootCauseDecision,
    ValidationAttestationRequest,
    ValidatorAssignmentStatus,
)
from app.schemas.validator_consensus import ValidationDisputeStatus
from app.schemas.validator_reward import (
    ValidatorRewardCycleCreateRequest,
    ValidatorRewardPolicyV1,
    ValidatorRewardProcessingStatus,
    ValidatorRewardVerificationStatus,
)
from app.schemas.week7_reward_cycle import Week7TaskRewardCycleCreateRequest
from app.services.reward_pool_consumption_service import load_reward_pool_consumption
from app.services.task_reward_budget_service import (
    create_task_reward_budget,
    finalize_task_reward_budget,
)
from app.services.validator_performance_service import evaluate_resolved_consensus_performance
from app.services.node_registry_service import load_node, save_node
from app.services.validation_attestation_service import create_validation_attestation
from app.services.validator_consensus_service import (
    calculate_validation_consensus,
    escalate_validation_dispute,
    finalize_validation_consensus,
    get_validation_dispute_path,
    list_validation_disputes,
)
from app.services.validator_reward_cycle_service import (
    ValidatorRewardCycleConflictError,
    ValidatorRewardCycleStateError,
    ValidatorRewardCycleSourceChangedError,
    calculate_validator_quality_factor,
    calculate_validator_reward_cycle,
    create_validator_reward_cycle,
    finalize_validator_reward_cycle,
    list_validator_reward_events,
    verify_validator_reward_cycle,
)
from app.services.week7_reward_cycle_service import (
    calculate_week7_task_reward_cycle,
    create_week7_task_reward_cycle,
    finalize_week7_task_reward_cycle,
    list_week7_reward_events,
)
from tests.test_validator_performance_service import _finalized_confirmed
from tests.test_validator_consensus_service import _create_evidence
from tests.test_validator_reproduction_execution_service import (
    _execute,
    _fake_executor,
    _finalized_context,
)
from app.utils.protocol_serialization import atomic_write_json
from app.services.validator_committee_service import load_validator_committee
from tests.test_task_operator_reward_service import _single_ready
from app.services.validator_assignment_service import get_assignment_path, load_validator_assignment


def _ready(tmp_path, monkeypatch, *, include_shadow=False, total="5000.000000"):
    root, workspace, routing, cluster, committee, consensus = _finalized_confirmed(
        tmp_path, monkeypatch, include_shadow=include_shadow
    )
    evaluate_resolved_consensus_performance(
        root,
        workspace,
        project_id="project-1",
        routing_id=routing.routing_id,
        final_validation_consensus_id=consensus.validation_consensus_id,
    )
    budget, _ = create_task_reward_budget(
        root,
        workspace,
        "project-1",
        TaskRewardBudgetCreateRequest(
            routing_id=routing.routing_id,
            total_budget_points=Decimal(total),
        ),
    )
    budget, _ = finalize_task_reward_budget(
        root, "project-1", budget.task_reward_budget_id
    )
    cycle, _ = create_validator_reward_cycle(
        root,
        project_id="project-1",
        routing_id=routing.routing_id,
        request=ValidatorRewardCycleCreateRequest(
            task_reward_budget_id=budget.task_reward_budget_id
        ),
    )
    return root, workspace, routing, cluster, committee, consensus, budget, cycle


def test_policy_is_decimal_exact_and_rejects_invalid_configuration():
    policy = ValidatorRewardPolicyV1()
    assert policy.completion_share == Decimal("0.300000")
    assert policy.quality_share == Decimal("0.700000")
    assert policy.completion_share + policy.quality_share == Decimal("1.000000")
    assert policy.quality_exponent == 2
    assert policy.shadow_reward_eligible is False
    with pytest.raises(ValidationError):
        ValidatorRewardPolicyV1(completion_share=Decimal("0.40"))
    with pytest.raises(ValidationError):
        ValidatorRewardPolicyV1(completion_share=0.30)


@pytest.mark.parametrize(
    ("quality", "factor"),
    [
        ("1.000000", "1.000000"),
        ("0.900000", "0.810000"),
        ("0.800000", "0.640000"),
        ("0.500000", "0.250000"),
        ("0.000000", "0.000000"),
    ],
)
def test_quality_factor_is_exact_decimal_square(quality, factor):
    assert calculate_validator_quality_factor(Decimal(quality)) == Decimal(factor)
    with pytest.raises(ValidatorRewardCycleConflictError):
        calculate_validator_quality_factor(float(quality))


def test_five_equal_authoritative_units_use_current_vq_only(tmp_path, monkeypatch):
    root, _, routing, _, committee, _, budget, cycle = _ready(
        tmp_path, monkeypatch, include_shadow=True
    )
    calculated, _ = calculate_validator_reward_cycle(
        root,
        project_id="project-1",
        routing_id=routing.routing_id,
        reward_cycle_id=cycle.reward_cycle_id,
    )
    assert calculated.status == RewardCycleStatus.CALCULATED
    assert calculated.authoritative_work_units == 5
    assert len(calculated.allocations) == 5
    assert {item.validator_assignment_id for item in calculated.allocations} == {
        seat.validator_assignment_id for seat in committee.authoritative_seats
    }
    assert committee.shadow_seats[0].validator_assignment_id not in {
        item.validator_assignment_id for item in calculated.allocations
    }
    assert {item.base_work_unit_budget for item in calculated.allocations} == {
        Decimal("200.000000")
    }
    by_assignment = {
        item.validator_assignment_id: item for item in calculated.allocations
    }
    expected_vq = ["1.000000", "0.850000", "0.800000", "1.000000", "1.000000"]
    expected_total = ["200.000000", "161.150000", "149.600000", "200.000000", "200.000000"]
    for seat, vq, total in zip(committee.authoritative_seats, expected_vq, expected_total):
        allocation = by_assignment[seat.validator_assignment_id]
        assert allocation.validation_quality_score == Decimal(vq)
        assert allocation.total_reward == Decimal(total)
        assert allocation.completion_reward == Decimal("60.000000")
    assert sum((item.base_work_unit_budget for item in calculated.allocations), Decimal("0")) == budget.validator_pool_points
    assert calculated.distributed_validator_points + calculated.undistributed_validator_points == budget.validator_pool_points
    node = load_node(root, committee.authoritative_seats[0].validator_node_id)
    save_node(root, node.model_copy(update={"reputation_score": 0.01}))
    repeated, repeated_status = calculate_validator_reward_cycle(
        root, project_id="project-1", routing_id=routing.routing_id,
        reward_cycle_id=cycle.reward_cycle_id,
    )
    assert repeated_status == ValidatorRewardProcessingStatus.UNCHANGED
    assert repeated.calculation_fingerprint == calculated.calculation_fingerprint
    verification = verify_validator_reward_cycle(
        root, project_id="project-1", routing_id=routing.routing_id,
        reward_cycle_id=cycle.reward_cycle_id,
    )
    assert verification.ok
    assert verification.verification_status == ValidatorRewardVerificationStatus.CALCULATED_NOT_FINALIZED


def test_finalize_consumes_only_validator_stream_and_is_idempotent(tmp_path, monkeypatch):
    root, _, routing, _, _, _, budget, cycle = _ready(tmp_path, monkeypatch)
    cycle, _ = calculate_validator_reward_cycle(
        root, project_id="project-1", routing_id=routing.routing_id, reward_cycle_id=cycle.reward_cycle_id
    )
    finalized, _, created, _ = finalize_validator_reward_cycle(
        root, project_id="project-1", routing_id=routing.routing_id, reward_cycle_id=cycle.reward_cycle_id
    )
    assert finalized.status == RewardCycleStatus.FINALIZED
    assert created == 5
    assert len(list_validator_reward_events(root, "project-1", routing.routing_id, cycle.reward_cycle_id)) == 5
    consumption = load_reward_pool_consumption(root, budget.task_reward_budget_id, RewardPoolKind.VALIDATOR)
    assert consumption is not None and consumption.reward_cycle_id == cycle.reward_cycle_id
    assert load_reward_pool_consumption(root, budget.task_reward_budget_id, RewardPoolKind.MINER) is None
    assert load_reward_pool_consumption(root, budget.task_reward_budget_id, RewardPoolKind.PROTOCOL) is None
    repeated, _, repeated_created, existing = finalize_validator_reward_cycle(
        root, project_id="project-1", routing_id=routing.routing_id, reward_cycle_id=cycle.reward_cycle_id
    )
    assert repeated == finalized
    assert repeated_created == 0 and existing == 5
    verification = verify_validator_reward_cycle(
        root, project_id="project-1", routing_id=routing.routing_id, reward_cycle_id=cycle.reward_cycle_id
    )
    assert verification.ok
    assert verification.verification_status == ValidatorRewardVerificationStatus.CLEAN_FINALIZED
    with pytest.raises(ValidatorRewardCycleConflictError):
        create_validator_reward_cycle(
            root,
            project_id="project-1",
            routing_id=routing.routing_id,
            request=ValidatorRewardCycleCreateRequest(task_reward_budget_id=budget.task_reward_budget_id),
            policy=ValidatorRewardPolicyV1(completion_share=Decimal("0.300001"), quality_share=Decimal("0.699999")),
        )


def test_missing_day5_assessment_blocks_resolved_reward(tmp_path, monkeypatch):
    root, workspace, routing, _, _, consensus = _finalized_confirmed(tmp_path, monkeypatch)
    budget, _ = create_task_reward_budget(
        root, workspace, "project-1",
        TaskRewardBudgetCreateRequest(routing_id=routing.routing_id, total_budget_points=Decimal("5000.000000")),
    )
    budget, _ = finalize_task_reward_budget(root, "project-1", budget.task_reward_budget_id)
    cycle, _ = create_validator_reward_cycle(
        root, project_id="project-1", routing_id=routing.routing_id,
        request=ValidatorRewardCycleCreateRequest(task_reward_budget_id=budget.task_reward_budget_id),
    )
    with pytest.raises(ValidatorRewardCycleStateError, match="Day 5"):
        calculate_validator_reward_cycle(
            root, project_id="project-1", routing_id=routing.routing_id, reward_cycle_id=cycle.reward_cycle_id
        )


def test_assignment_change_stales_calculated_cycle(tmp_path, monkeypatch):
    root, _, routing, _, committee, _, _, cycle = _ready(tmp_path, monkeypatch)
    cycle, _ = calculate_validator_reward_cycle(
        root, project_id="project-1", routing_id=routing.routing_id,
        reward_cycle_id=cycle.reward_cycle_id,
    )
    assignment_id = committee.authoritative_seats[0].validator_assignment_id
    assignment = load_validator_assignment(root, "project-1", routing.routing_id, assignment_id)
    atomic_write_json(
        get_assignment_path(root, routing.routing_id, assignment_id),
        assignment.model_copy(
            update={
                "status": ValidatorAssignmentStatus.CANCELLED,
                "attested_at": None,
            }
        ),
    )
    with pytest.raises(ValidatorRewardCycleSourceChangedError, match="source changed"):
        finalize_validator_reward_cycle(
            root, project_id="project-1", routing_id=routing.routing_id,
            reward_cycle_id=cycle.reward_cycle_id,
        )


def test_terminal_unresolved_work_receives_completion_only(tmp_path, monkeypatch):
    root, workspace, routing, cluster, _ = _create_evidence(
        tmp_path,
        monkeypatch,
        [{"validity": ValidationStatus.ACCEPTED}] * 3
        + [{"validity": ValidationStatus.REJECTED}] * 2,
    )
    consensus = calculate_validation_consensus(
        root, workspace, project_id="project-1", routing_id=routing.routing_id,
        finding_cluster_id=cluster.finding_cluster_id,
    )
    consensus = finalize_validation_consensus(
        root, workspace, project_id="project-1", routing_id=routing.routing_id,
        validation_consensus_id=consensus.validation_consensus_id,
    )
    dispute = list_validation_disputes(
        root, "project-1", routing.routing_id, finding_cluster_id=cluster.finding_cluster_id
    )[0]
    atomic_write_json(
        get_validation_dispute_path(root, routing.routing_id, dispute.validation_dispute_id),
        dispute.model_copy(update={"status": ValidationDisputeStatus.UNRESOLVED}),
    )
    budget, _ = create_task_reward_budget(
        root, workspace, "project-1",
        TaskRewardBudgetCreateRequest(routing_id=routing.routing_id, total_budget_points=Decimal("5000.000000")),
    )
    budget, _ = finalize_task_reward_budget(root, "project-1", budget.task_reward_budget_id)
    cycle, _ = create_validator_reward_cycle(
        root, project_id="project-1", routing_id=routing.routing_id,
        request=ValidatorRewardCycleCreateRequest(task_reward_budget_id=budget.task_reward_budget_id),
    )
    calculated, _ = calculate_validator_reward_cycle(
        root, project_id="project-1", routing_id=routing.routing_id, reward_cycle_id=cycle.reward_cycle_id
    )
    assert calculated.unresolved_units == 5
    assert all(item.completion_reward == Decimal("60.000000") for item in calculated.allocations)
    assert all(item.quality_reward == 0 for item in calculated.allocations)
    assert all(item.total_reward == Decimal("60.000000") for item in calculated.allocations)
    assert calculated.distributed_validator_points == Decimal("300.000000")
    assert calculated.undistributed_validator_points == Decimal("700.000000")


def test_incomplete_authoritative_seat_reserves_budget_without_peer_redistribution(tmp_path, monkeypatch):
    root, workspace, routing, cluster, committee = _finalized_context(tmp_path)
    statuses = {
        seat.validator_node_id: ReproductionStatus.REPRODUCED
        for seat in committee.authoritative_seats[:4]
    }
    current = _fake_executor(statuses, monkeypatch)
    for seat in committee.authoritative_seats[:4]:
        current["value"] = seat.validator_node_id
        reproduction = _execute(root, workspace, routing, seat)
        create_validation_attestation(
            root, workspace, project_id="project-1", routing_id=routing.routing_id,
            validator_assignment_id=seat.validator_assignment_id,
            request=ValidationAttestationRequest(
                validator_node_id=seat.validator_node_id,
                validator_reproduction_id=reproduction.validator_reproduction_id,
                validity_decision=ValidationStatus.ACCEPTED,
                root_cause_decision=RootCauseDecision.CONFIRMED,
                normalized_severity="High",
                impact_decision=ImpactDecision.VALIDATED,
            ),
        )
    consensus = calculate_validation_consensus(
        root, workspace, project_id="project-1", routing_id=routing.routing_id,
        finding_cluster_id=cluster.finding_cluster_id,
    )
    consensus = finalize_validation_consensus(
        root, workspace, project_id="project-1", routing_id=routing.routing_id,
        validation_consensus_id=consensus.validation_consensus_id,
    )
    evaluate_resolved_consensus_performance(
        root, workspace, project_id="project-1", routing_id=routing.routing_id,
        final_validation_consensus_id=consensus.validation_consensus_id,
    )
    budget, _ = create_task_reward_budget(
        root, workspace, "project-1",
        TaskRewardBudgetCreateRequest(routing_id=routing.routing_id, total_budget_points=Decimal("5000.000000")),
    )
    budget, _ = finalize_task_reward_budget(root, "project-1", budget.task_reward_budget_id)
    cycle, _ = create_validator_reward_cycle(
        root, project_id="project-1", routing_id=routing.routing_id,
        request=ValidatorRewardCycleCreateRequest(task_reward_budget_id=budget.task_reward_budget_id),
    )
    calculated, _ = calculate_validator_reward_cycle(
        root, project_id="project-1", routing_id=routing.routing_id, reward_cycle_id=cycle.reward_cycle_id
    )
    assert calculated.authoritative_work_units == 5
    assert {item.base_work_unit_budget for item in calculated.allocations} == {Decimal("200.000000")}
    no_show = next(item for item in calculated.allocations if item.validator_assignment_id == committee.authoritative_seats[4].validator_assignment_id)
    assert not no_show.completion_eligible
    assert no_show.total_reward == 0
    assert no_show.undistributed_points == Decimal("200.000000")
    finalized, _, created, _ = finalize_validator_reward_cycle(
        root, project_id="project-1", routing_id=routing.routing_id,
        reward_cycle_id=cycle.reward_cycle_id,
    )
    assert created == 4
    assert finalized.reward_event_count == 4
    assert no_show.validator_assignment_id not in {
        event.validator_assignment_id
        for event in list_validator_reward_events(
            root, "project-1", routing.routing_id, cycle.reward_cycle_id
        )
    }


def test_partial_event_publication_and_consumption_status_are_retry_safe(tmp_path, monkeypatch):
    root, _, routing, _, _, _, budget, cycle = _ready(tmp_path, monkeypatch)
    cycle, _ = calculate_validator_reward_cycle(
        root, project_id="project-1", routing_id=routing.routing_id,
        reward_cycle_id=cycle.reward_cycle_id,
    )
    with pytest.raises(RuntimeError, match="publication"):
        finalize_validator_reward_cycle(
            root, project_id="project-1", routing_id=routing.routing_id,
            reward_cycle_id=cycle.reward_cycle_id, fail_after_event_writes=2,
        )
    partial = verify_validator_reward_cycle(
        root, project_id="project-1", routing_id=routing.routing_id,
        reward_cycle_id=cycle.reward_cycle_id,
    )
    assert partial.verification_status == ValidatorRewardVerificationStatus.PARTIAL_EVENT_PUBLICATION
    assert partial.safe_retry_finalize
    finalized, _, created, existing = finalize_validator_reward_cycle(
        root, project_id="project-1", routing_id=routing.routing_id,
        reward_cycle_id=cycle.reward_cycle_id,
    )
    assert finalized.status == RewardCycleStatus.FINALIZED
    assert created == 3 and existing == 2

    # A separate snapshot demonstrates recovery after events and pool marker are
    # durable but before the cycle's final status write.
    root2, _, routing2, _, _, _, budget2, cycle2 = _ready(
        tmp_path / "second", monkeypatch
    )
    cycle2, _ = calculate_validator_reward_cycle(
        root2, project_id="project-1", routing_id=routing2.routing_id,
        reward_cycle_id=cycle2.reward_cycle_id,
    )
    with pytest.raises(RuntimeError, match="consumption"):
        finalize_validator_reward_cycle(
            root2, project_id="project-1", routing_id=routing2.routing_id,
            reward_cycle_id=cycle2.reward_cycle_id, fail_after_consumption=True,
        )
    assert load_reward_pool_consumption(
        root2, budget2.task_reward_budget_id, RewardPoolKind.VALIDATOR
    ) is not None
    recovered, _, created, existing = finalize_validator_reward_cycle(
        root2, project_id="project-1", routing_id=routing2.routing_id,
        reward_cycle_id=cycle2.reward_cycle_id,
    )
    assert recovered.status == RewardCycleStatus.FINALIZED
    assert created == 0 and existing == 5


def test_escalation_adds_equal_work_units_without_increasing_pool(tmp_path, monkeypatch):
    root, workspace, routing, cluster, _ = _create_evidence(
        tmp_path,
        monkeypatch,
        [{"validity": ValidationStatus.ACCEPTED}] * 3
        + [{"validity": ValidationStatus.REJECTED}] * 2,
        validator_count=10,
    )
    round_one = calculate_validation_consensus(
        root, workspace, project_id="project-1", routing_id=routing.routing_id,
        finding_cluster_id=cluster.finding_cluster_id,
    )
    finalize_validation_consensus(
        root, workspace, project_id="project-1", routing_id=routing.routing_id,
        validation_consensus_id=round_one.validation_consensus_id,
    )
    dispute = list_validation_disputes(
        root, "project-1", routing.routing_id, finding_cluster_id=cluster.finding_cluster_id
    )[0]
    escalation = escalate_validation_dispute(
        root, project_id="project-1", routing_id=routing.routing_id,
        validation_dispute_id=dispute.validation_dispute_id,
    )
    committee = load_validator_committee(
        root, "project-1", routing.routing_id, escalation.validator_committee_id
    )
    statuses = {
        seat.validator_node_id: ReproductionStatus.REPRODUCED
        for seat in committee.authoritative_seats
    }
    current = _fake_executor(statuses, monkeypatch)
    for seat in committee.authoritative_seats:
        current["value"] = seat.validator_node_id
        reproduction = _execute(root, workspace, routing, seat)
        create_validation_attestation(
            root, workspace, project_id="project-1", routing_id=routing.routing_id,
            validator_assignment_id=seat.validator_assignment_id,
            request=ValidationAttestationRequest(
                validator_node_id=seat.validator_node_id,
                validator_reproduction_id=reproduction.validator_reproduction_id,
                validity_decision=ValidationStatus.ACCEPTED,
                root_cause_decision=RootCauseDecision.CONFIRMED,
                normalized_severity="High",
                impact_decision=ImpactDecision.VALIDATED,
            ),
        )
    cumulative = calculate_validation_consensus(
        root, workspace, project_id="project-1", routing_id=routing.routing_id,
        finding_cluster_id=cluster.finding_cluster_id,
    )
    cumulative = finalize_validation_consensus(
        root, workspace, project_id="project-1", routing_id=routing.routing_id,
        validation_consensus_id=cumulative.validation_consensus_id,
    )
    evaluate_resolved_consensus_performance(
        root, workspace, project_id="project-1", routing_id=routing.routing_id,
        final_validation_consensus_id=cumulative.validation_consensus_id,
    )
    budget, _ = create_task_reward_budget(
        root, workspace, "project-1",
        TaskRewardBudgetCreateRequest(routing_id=routing.routing_id, total_budget_points=Decimal("5000.000005")),
    )
    budget, _ = finalize_task_reward_budget(root, "project-1", budget.task_reward_budget_id)
    cycle, _ = create_validator_reward_cycle(
        root, project_id="project-1", routing_id=routing.routing_id,
        request=ValidatorRewardCycleCreateRequest(task_reward_budget_id=budget.task_reward_budget_id),
    )
    cycle, _ = calculate_validator_reward_cycle(
        root, project_id="project-1", routing_id=routing.routing_id,
        reward_cycle_id=cycle.reward_cycle_id,
    )
    assert cycle.authoritative_work_units == 9
    bases = [item.base_work_unit_budget for item in cycle.allocations]
    assert sum(bases, Decimal("0")) == budget.validator_pool_points
    assert max(bases) - min(bases) <= Decimal("0.000001")
    assert all(item.work_unit_weight == Decimal("1.000000") for item in cycle.allocations)


def test_finalized_miner_and_validator_streams_coexist_without_rewrite(tmp_path, monkeypatch):
    root, workspace, routing, _, _, _, budget, validator = _ready(
        tmp_path, monkeypatch
    )
    miner, _ = create_week7_task_reward_cycle(
        root, workspace, "project-1", routing.routing_id,
        Week7TaskRewardCycleCreateRequest(task_reward_budget_id=budget.task_reward_budget_id),
    )
    miner, _ = calculate_week7_task_reward_cycle(root, "project-1", miner.reward_cycle_id)
    miner = finalize_week7_task_reward_cycle(root, "project-1", miner.reward_cycle_id).cycle
    miner_events_before = [
        item.model_dump()
        for item in list_week7_reward_events(
            root, reward_cycle_id=miner.reward_cycle_id
        )
    ]

    validator, _ = calculate_validator_reward_cycle(
        root, project_id="project-1", routing_id=routing.routing_id,
        reward_cycle_id=validator.reward_cycle_id,
    )
    assert validator.authoritative_work_units == 5
    validator, _, created, _ = finalize_validator_reward_cycle(
        root, project_id="project-1", routing_id=routing.routing_id,
        reward_cycle_id=validator.reward_cycle_id,
    )
    assert created == 5
    assert validator.distributed_validator_points > 0
    assert validator.distributed_validator_points + validator.undistributed_validator_points == budget.validator_pool_points
    assert [
        item.model_dump()
        for item in list_week7_reward_events(
            root, reward_cycle_id=miner.reward_cycle_id
        )
    ] == miner_events_before
    assert load_reward_pool_consumption(root, budget.task_reward_budget_id, RewardPoolKind.VALIDATOR) is not None
    assert load_reward_pool_consumption(root, budget.task_reward_budget_id, RewardPoolKind.MINER) is None


def test_legacy_cluster_without_validator_flow_is_not_retroactively_rewarded(tmp_path):
    root, _, routing, _, budget, _ = _single_ready(tmp_path)
    cycle, _ = create_validator_reward_cycle(
        root, project_id="project-1", routing_id=routing.routing_id,
        request=ValidatorRewardCycleCreateRequest(task_reward_budget_id=budget.task_reward_budget_id),
    )
    with pytest.raises(ValidatorRewardCycleStateError, match="every finding cluster"):
        calculate_validator_reward_cycle(
            root, project_id="project-1", routing_id=routing.routing_id,
            reward_cycle_id=cycle.reward_cycle_id,
        )
