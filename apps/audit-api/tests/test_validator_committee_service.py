from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from app.schemas.node import NodeCreate, NodeStatusChangeRequest
from app.schemas.validator_attestation import (
    ValidatorAssignment,
    ValidatorAssignmentRole,
    ValidatorAssignmentStatus,
)
from app.schemas.validator_committee import (
    ValidationAssuranceMode,
    ValidatorCommitteePolicyV1,
    ValidatorCommitteeStatus,
)
from app.services.finding_cluster_service import (
    finalize_finding_clusters_for_task,
    load_finding_cluster,
    rebuild_finding_clusters_for_task,
)
from app.services.node_registry_service import (
    change_node_status,
    create_node,
    load_node,
    save_node,
)
from app.services.validator_assignment_service import (
    get_assignment_path,
    list_validator_assignments,
    update_assignment_status,
)
from app.utils.protocol_serialization import atomic_write_json
from app.services.validator_committee_service import (
    ValidatorCommitteeInsufficientValidatorsError,
    ValidatorCommitteeStaleError,
    finalize_validator_committee,
    list_validator_assignment_usage_events,
    load_validator_committee,
    plan_validator_committee,
)
from tests.test_subnet_reward_allocation_service import _setup


NOW = datetime(2026, 8, 21, 12, tzinfo=timezone.utc)


def _validator(root, index, *, operator_id=None, node_type="validator", categories=None):
    return create_node(
        root,
        NodeCreate(
            node_type=node_type,
            display_name=f"Committee Validator {index}",
            operator_id=operator_id or f"committee-operator-{str(index).zfill(2)}",
            supported_categories=(
                categories
                if categories is not None
                else ["access_control"]
            ),
        ),
    )


def _context(tmp_path, *, validator_count=6, include_candidate=False):
    root, workspace, _, routing, _ = _setup(
        tmp_path, include_candidate=include_candidate
    )
    clusters = rebuild_finding_clusters_for_task(
        root, workspace, "project-1", routing.routing_id
    ).clusters
    clusters = finalize_finding_clusters_for_task(
        root, "project-1", routing.routing_id
    )
    validators = [_validator(root, index) for index in range(validator_count)]
    return root, workspace, routing, clusters, validators


def _plan(root, routing, cluster, **kwargs):
    return plan_validator_committee(
        root,
        project_id="project-1",
        routing_id=routing.routing_id,
        finding_cluster_id=cluster.finding_cluster_id,
        calculated_at=NOW,
        **kwargs,
    )


def _assignment_history(root, routing, validator, count, *, status="assigned"):
    for index in range(count):
        assignment_id = f"history_{validator.node_id}_{index}"
        assignment = ValidatorAssignment(
            validator_assignment_id=assignment_id,
            project_id="project-1",
            routing_id=routing.routing_id,
            finding_cluster_id=f"history-cluster-{validator.node_id}-{index}",
            validator_node_id=validator.node_id,
            validator_operator_id=validator.operator_id,
            category="access_control",
            assignment_role=ValidatorAssignmentRole.AUTHORITATIVE,
            status=status,
            source_fingerprint="0" * 64,
            assigned_at=NOW - timedelta(days=index),
            updated_at=NOW - timedelta(days=index),
        )
        atomic_write_json(
            get_assignment_path(root, routing.routing_id, assignment_id), assignment
        )


def test_policy_defaults_are_versioned_five_seven_and_separate_shadow_slots():
    policy = ValidatorCommitteePolicyV1()
    assert policy.policy_version == "validator_committee_policy_v1"
    assert policy.authoritative_size(ValidationAssuranceMode.STANDARD) == 5
    assert policy.authoritative_size(ValidationAssuranceMode.HIGH_ASSURANCE) == 7
    assert policy.shadow_slots(ValidationAssuranceMode.STANDARD) == 1
    assert policy.shadow_slots_required is False
    assert policy.max_concurrent_validator_assignments_per_node == 5


def test_day2_rejects_round_two_selection_but_identity_is_round_ready(tmp_path):
    root, _, routing, clusters, _ = _context(tmp_path, validator_count=6)
    with pytest.raises(ValueError, match="validation_round 1"):
        _plan(root, routing, clusters[0], validation_round=2)


def test_standard_plan_is_deterministic_diverse_and_category_capable(tmp_path):
    root, _, routing, clusters, _ = _context(tmp_path, validator_count=7)
    first = _plan(root, routing, clusters[0])
    repeated = _plan(root, routing, clusters[0])
    assert repeated == first
    assert first.status == ValidatorCommitteeStatus.PLANNED
    assert len(first.authoritative_seats) == 5
    assert len(first.shadow_seats) == 1
    assert [seat.seat_index for seat in first.authoritative_seats] == [1, 2, 3, 4, 5]
    all_seats = first.authoritative_seats + first.shadow_seats
    assert len({seat.operator_id for seat in all_seats}) == 6
    assert all(
        first.category in rank.supported_categories for rank in first.candidate_ranks
    )
    assert all(rank.validator_skill_score is None for rank in first.candidate_ranks)


def test_candidate_input_order_and_generated_calculation_time_do_not_change_result(
    tmp_path, monkeypatch
):
    root, _, routing, clusters, _ = _context(tmp_path, validator_count=6)
    first = _plan(root, routing, clusters[0])
    from app.services import validator_committee_service as committee_service

    original = committee_service.list_nodes
    monkeypatch.setattr(
        committee_service,
        "list_nodes",
        lambda protocol_root: list(reversed(original(protocol_root))),
    )
    repeated = plan_validator_committee(
        root,
        project_id="project-1",
        routing_id=routing.routing_id,
        finding_cluster_id=clusters[0].finding_cluster_id,
        calculated_at=NOW + timedelta(hours=1),
    )
    assert repeated.source_fingerprint == first.source_fingerprint
    assert repeated.authoritative_seats == first.authoritative_seats


def test_high_assurance_requires_exactly_seven_without_downgrade(tmp_path):
    root, _, routing, clusters, _ = _context(tmp_path, validator_count=8)
    committee = _plan(
        root,
        routing,
        clusters[0],
        assurance_mode=ValidationAssuranceMode.HIGH_ASSURANCE,
    )
    assert committee.authoritative_target_size == 7
    assert len(committee.authoritative_seats) == 7
    assert len({seat.operator_id for seat in committee.authoritative_seats}) == 7
    assert committee.assurance_mode == ValidationAssuranceMode.HIGH_ASSURANCE


@pytest.mark.parametrize(
    ("mode", "validator_count", "required"),
    [
        (ValidationAssuranceMode.STANDARD, 4, 5),
        (ValidationAssuranceMode.HIGH_ASSURANCE, 6, 7),
    ],
)
def test_insufficient_independent_operators_fail_closed_without_side_effects(
    tmp_path, mode, validator_count, required
):
    root, _, routing, clusters, _ = _context(
        tmp_path, validator_count=validator_count
    )
    with pytest.raises(ValidatorCommitteeInsufficientValidatorsError) as failure:
        _plan(root, routing, clusters[0], assurance_mode=mode)
    assert failure.value.required == required
    assert list_validator_assignments(root, "project-1", routing.routing_id) == []
    assert list_validator_assignment_usage_events(root) == []


def test_many_nodes_from_four_operators_cannot_fill_five_seats(tmp_path):
    root, _, routing, clusters, _ = _context(tmp_path, validator_count=0)
    for operator in range(4):
        for node in range(3):
            _validator(root, f"{operator}-{node}", operator_id=f"shared-{operator}")
    with pytest.raises(ValidatorCommitteeInsufficientValidatorsError) as failure:
        _plan(root, routing, clusters[0])
    assert failure.value.available == 4


def test_same_operator_gets_one_seat_across_authoritative_and_shadow(tmp_path):
    root, _, routing, clusters, _ = _context(tmp_path, validator_count=5)
    for index in range(3):
        _validator(root, f"shared-{index}", operator_id="multi-node-operator")
    committee = _plan(root, routing, clusters[0])
    seats = committee.authoritative_seats + committee.shadow_seats
    assert sum(seat.operator_id == "multi-node-operator" for seat in seats) <= 1
    assert len({seat.operator_id for seat in seats}) == len(seats)


def test_representative_node_uses_lower_workload_then_node_id(tmp_path):
    root, _, routing, clusters, _ = _context(tmp_path, validator_count=5)
    nodes = [
        _validator(root, f"representative-{index}", operator_id="representative-op")
        for index in range(3)
    ]
    for node, workload in zip(nodes, (4, 1, 2), strict=True):
        _assignment_history(root, routing, node, workload)
    committee = _plan(root, routing, clusters[0])
    representative = next(
        seat
        for seat in committee.authoritative_seats + committee.shadow_seats
        if seat.operator_id == "representative-op"
    )
    assert representative.validator_node_id == nodes[1].node_id


def test_node_at_configured_concurrent_capacity_is_excluded(tmp_path):
    root, _, routing, clusters, validators = _context(tmp_path, validator_count=6)
    _assignment_history(root, routing, validators[0], 5)
    committee = _plan(root, routing, clusters[0])
    assert validators[0].node_id not in {
        seat.validator_node_id
        for seat in committee.authoritative_seats + committee.shadow_seats
    }
    assert len(committee.authoritative_seats) == 5


@pytest.mark.parametrize("status", ["inactive", "suspended", "banned"])
def test_non_active_validator_is_excluded(tmp_path, status):
    root, _, routing, clusters, validators = _context(tmp_path, validator_count=6)
    change_node_status(
        root,
        validators[0].node_id,
        NodeStatusChangeRequest(status=status, reason="Committee eligibility test."),
    )
    committee = _plan(root, routing, clusters[0])
    assert validators[0].node_id not in {
        seat.validator_node_id
        for seat in committee.authoritative_seats + committee.shadow_seats
    }


def test_agent_unsupported_category_and_reporting_operator_are_excluded(tmp_path):
    root, _, routing, clusters, _ = _context(tmp_path, validator_count=6)
    cluster = clusters[0]
    reporter_operator = load_node(root, cluster.members[0].node_id).operator_id
    conflicted = _validator(
        root,
        "conflicted",
        operator_id=reporter_operator,
        node_type="hybrid",
    )
    unsupported = _validator(root, "unsupported", categories=["reentrancy"])
    agent = _validator(root, "agent", node_type="agent")
    committee = _plan(root, routing, cluster)
    selected = {
        seat.validator_node_id
        for seat in committee.authoritative_seats + committee.shadow_seats
    }
    assert conflicted.node_id not in selected
    assert unsupported.node_id not in selected
    assert agent.node_id not in selected
    assert reporter_operator not in {seat.operator_id for seat in committee.authoritative_seats}


def test_registry_operator_mapping_not_cached_cluster_operator_drives_conflict(tmp_path):
    root, _, routing, clusters, validators = _context(tmp_path, validator_count=6)
    reporter = load_node(root, clusters[0].members[0].node_id)
    save_node(root, reporter.model_copy(update={"operator_id": validators[0].operator_id}))
    committee = _plan(root, routing, clusters[0])
    assert validators[0].operator_id not in {
        seat.operator_id for seat in committee.authoritative_seats
    }


def test_shadow_is_additional_and_optional_when_only_five_operators_exist(tmp_path):
    root, _, routing, clusters, _ = _context(tmp_path, validator_count=5)
    committee = _plan(root, routing, clusters[0])
    assert len(committee.authoritative_seats) == 5
    assert committee.shadow_target_size == 1
    assert committee.actual_shadow_size == 0
    finalized = finalize_validator_committee(
        root,
        project_id="project-1",
        routing_id=routing.routing_id,
        validator_committee_id=committee.validator_committee_id,
    )
    assert finalized.committee.status == ValidatorCommitteeStatus.FINALIZED


def test_finalization_creates_linked_roles_and_separate_usage_exactly_once(tmp_path):
    root, _, routing, clusters, _ = _context(tmp_path, validator_count=6)
    cluster_before = deepcopy(clusters[0])
    committee = _plan(root, routing, clusters[0])
    result = finalize_validator_committee(
        root,
        project_id="project-1",
        routing_id=routing.routing_id,
        validator_committee_id=committee.validator_committee_id,
    )
    assert result.assignments_created == 6
    assert result.usage_events_created == 6
    assignments = list_validator_assignments(
        root,
        "project-1",
        routing.routing_id,
        finding_cluster_id=clusters[0].finding_cluster_id,
    )
    assert len(assignments) == 6
    assert all(item.validator_committee_id == committee.validator_committee_id for item in assignments)
    assert sum(item.assignment_role == ValidatorAssignmentRole.AUTHORITATIVE for item in assignments) == 5
    assert sum(item.assignment_role == ValidatorAssignmentRole.SHADOW for item in assignments) == 1
    assert all(item.status == ValidatorAssignmentStatus.ASSIGNED for item in assignments)
    usage = list_validator_assignment_usage_events(root)
    assert len(usage) == 6
    assert {event.role for event in usage} == {
        ValidatorAssignmentRole.AUTHORITATIVE,
        ValidatorAssignmentRole.SHADOW,
    }
    repeated = finalize_validator_committee(
        root,
        project_id="project-1",
        routing_id=routing.routing_id,
        validator_committee_id=committee.validator_committee_id,
    )
    assert repeated.assignments_created == repeated.usage_events_created == 0
    assert len(list_validator_assignment_usage_events(root)) == 6
    assert not (root / "routing" / "usage" / assignments[0].validator_node_id).exists()
    assert not (root / "validator-consensus").exists()
    assert not (root / "validator-rewards").exists()
    assert not (root / "validator-protocol" / "reproductions").exists()
    assert not (root / "validator-protocol" / "attestations").exists()
    assert (
        load_finding_cluster(
            root, "project-1", routing.routing_id, clusters[0].finding_cluster_id
        )
        == cluster_before
    )


@pytest.mark.parametrize("mutation", ["suspended", "inactive", "category", "operator"])
def test_selection_relevant_mutation_blocks_stale_finalization(tmp_path, mutation):
    root, _, routing, clusters, _ = _context(tmp_path, validator_count=6)
    committee = _plan(root, routing, clusters[0])
    selected = load_node(root, committee.authoritative_seats[0].validator_node_id)
    if mutation in {"suspended", "inactive"}:
        change_node_status(
            root,
            selected.node_id,
            NodeStatusChangeRequest(status=mutation, reason="Stale committee test."),
        )
    elif mutation == "category":
        save_node(root, selected.model_copy(update={"supported_categories": ["reentrancy"]}))
    else:
        save_node(root, selected.model_copy(update={"operator_id": "changed-operator"}))
    with pytest.raises(ValidatorCommitteeStaleError):
        finalize_validator_committee(
            root,
            project_id="project-1",
            routing_id=routing.routing_id,
            validator_committee_id=committee.validator_committee_id,
        )
    assert list_validator_assignments(root, "project-1", routing.routing_id) == []
    assert list_validator_assignment_usage_events(root) == []


def test_reputation_and_unrelated_agent_score_storage_do_not_change_selection(tmp_path):
    root, _, routing, clusters, validators = _context(tmp_path, validator_count=6)
    first = _plan(root, routing, clusters[0])
    save_node(
        root,
        validators[0].model_copy(update={"reputation_score": 0.99}),
    )
    score_path = root / "category-scores" / validators[0].node_id / "access_control.json"
    score_path.parent.mkdir(parents=True, exist_ok=True)
    score_path.write_text('{"agent_category_score": 0.999}\n', encoding="utf-8")
    repeated = _plan(root, routing, clusters[0])
    assert repeated.source_fingerprint == first.source_fingerprint
    assert repeated.authoritative_seats == first.authoritative_seats
    assert all(rank.validator_skill_score is None for rank in repeated.candidate_ranks)


def test_policy_target_change_changes_fingerprint_and_blocks_old_policy_finalize(tmp_path):
    root, _, routing, clusters, _ = _context(tmp_path, validator_count=7)
    first = _plan(root, routing, clusters[0])
    changed_policy = ValidatorCommitteePolicyV1(standard_authoritative_size=6)
    recalculated = _plan(root, routing, clusters[0], policy=changed_policy)
    assert recalculated.validator_committee_id == first.validator_committee_id
    assert recalculated.source_fingerprint != first.source_fingerprint
    assert len(recalculated.authoritative_seats) == 6
    with pytest.raises(ValidatorCommitteeStaleError):
        finalize_validator_committee(
            root,
            project_id="project-1",
            routing_id=routing.routing_id,
            validator_committee_id=recalculated.validator_committee_id,
        )


def test_lower_active_validator_workload_and_cold_start_shadow_are_preferred(tmp_path):
    root, _, routing, clusters, validators = _context(
        tmp_path, validator_count=7, include_candidate=True
    )
    assert len(clusters) >= 2
    prior = plan_validator_committee(
        root,
        project_id="project-1",
        routing_id=routing.routing_id,
        finding_cluster_id=clusters[0].finding_cluster_id,
        calculated_at=NOW,
    )
    prior_result = finalize_validator_committee(
        root,
        project_id="project-1",
        routing_id=routing.routing_id,
        validator_committee_id=prior.validator_committee_id,
    )
    loaded = load_validator_committee(
        root, "project-1", routing.routing_id, prior.validator_committee_id
    )
    assert loaded == prior_result.committee
    second = plan_validator_committee(
        root,
        project_id="project-1",
        routing_id=routing.routing_id,
        finding_cluster_id=clusters[1].finding_cluster_id,
        calculated_at=NOW,
    )
    prior_authoritative = {seat.operator_id for seat in prior.authoritative_seats}
    # The two operators not used authoritatively in round one have lower active load.
    assert {
        seat.operator_id for seat in second.authoritative_seats[:2]
    }.isdisjoint(prior_authoritative)
    assert second.shadow_seats[0].operator_id in prior_authoritative


def test_terminal_assignments_do_not_count_as_current_workload(tmp_path):
    root, _, routing, clusters, _ = _context(
        tmp_path, validator_count=7, include_candidate=True
    )
    first = _plan(root, routing, clusters[0])
    finalized = finalize_validator_committee(
        root,
        project_id="project-1",
        routing_id=routing.routing_id,
        validator_committee_id=first.validator_committee_id,
    )
    assignment = list_validator_assignments(
        root,
        "project-1",
        routing.routing_id,
        finding_cluster_id=clusters[0].finding_cluster_id,
    )[0]
    update_assignment_status(root, assignment, ValidatorAssignmentStatus.CANCELLED)
    second = _plan(root, routing, clusters[1])
    rank = next(
        item for item in second.candidate_ranks if item.validator_node_id == assignment.validator_node_id
    )
    assert rank.current_authoritative_assignments == 0
    assert rank.lifetime_authoritative_assignments == 1
    assert finalized.committee.status == ValidatorCommitteeStatus.FINALIZED
