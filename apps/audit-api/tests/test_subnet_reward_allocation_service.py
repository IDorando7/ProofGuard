import json
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.schemas.finding import Finding
from app.schemas.reproduction import ReproductionStatus
from app.schemas.routing import ProjectRoutingRequest
from app.schemas.submission import SubmissionCreate, SubmissionStatusUpdate
from app.schemas.subnet import SubnetCreate
from app.schemas.subnet_reward import (
    SubnetRewardCycleCreateRequest,
    SubnetRewardCycleStatus,
)
from app.schemas.validation import ValidationEvidence, ValidationStatus
from app.services.contribution_scoring_service import (
    calculate_contribution_for_submission,
    load_contribution_score,
)
from app.services.node_registry_service import load_node
from app.services.reproduction_service import (
    create_initial_reproduction_result,
    load_reproduction_result,
    save_reproduction_result,
)
from app.services.submission_service import (
    create_submission,
    load_submission,
    update_submission_status,
)
from app.services.subnet_registry_service import (
    create_subnet,
    load_subnet_member,
)
from app.services.subnet_reward_allocation_service import (
    SubnetRewardAlreadyFinalizedError,
    SubnetRewardCycleConflictError,
    SubnetRewardSourceChangedError,
    WeightedRewardItem,
    allocate_category_pools,
    allocate_proportional_rewards,
    calculate_subnet_reward_cycle,
    create_subnet_reward_cycle,
    finalize_subnet_reward_cycle,
    get_category_reward_multiplier,
    get_membership_reward_multiplier,
    get_subnet_reward_cycle_path,
    list_subnet_reward_events,
    load_subnet_reward_cycle,
)
from app.services.subnet_router_service import (
    calculate_project_routing,
    finalize_project_routing,
    load_routing_record,
)
from app.services.validation_service import (
    create_validation_decision,
    load_validation_decision,
)
from tests.test_subnet_router_service import _member, _node, _workspace


NOW = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)


def _finding(project_id, finding_id, category="access_control"):
    return Finding(
        finding_id=finding_id,
        project_id=project_id,
        title=f"Accepted routed finding {finding_id}",
        category=category,
        severity="High",
        confidence=0.9,
        contracts=["src/Vault.sol"],
        functions=[f"withdraw{finding_id}"],
        root_cause=f"Missing authorization {finding_id}",
        attack_path=f"Attacker invokes privileged path {finding_id}",
        impact=f"Unauthorized asset movement {finding_id}",
        recommended_fix="Add authorization.",
        agent_name="access_control_agent",
    )


def _write_findings(workspace, findings):
    directory = workspace / "findings"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "findings.json").write_text(
        json.dumps(
            [finding.model_dump(mode="json") for finding in findings],
            indent=2,
        ),
        encoding="utf-8",
    )


def _accepted_submission(root, workspace, routing, assignment, finding_id):
    finding = _finding(routing.project_id, finding_id)
    findings_path = workspace / "findings" / "findings.json"
    existing = (
        json.loads(findings_path.read_text(encoding="utf-8"))
        if findings_path.exists()
        else []
    )
    _write_findings(
        workspace,
        [
            Finding.model_validate(item) for item in existing
        ]
        + [finding],
    )
    submission = create_submission(
        root,
        workspace,
        SubmissionCreate(
            project_id=routing.project_id,
            finding_id=finding_id,
            node_id=assignment.node_id,
            routing_id=routing.routing_id,
            routing_assignment_id=assignment.assignment_id,
        ),
    )
    reproduction = create_initial_reproduction_result(
        routing.project_id,
        finding_id,
        workspace,
    )
    reproduction = save_reproduction_result(
        reproduction.model_copy(update={"status": ReproductionStatus.REPRODUCED}),
        workspace,
    )
    validation = create_validation_decision(
        routing.project_id,
        finding_id,
        workspace,
        status=ValidationStatus.ACCEPTED,
        reason="Accepted routed finding.",
        evidence=ValidationEvidence(
            has_finding=True,
            has_reproduction=True,
            reproduction_status="reproduced",
            in_scope=True,
            is_duplicate=False,
            normalized_severity="High",
        ),
    )
    submission = update_submission_status(
        root,
        submission.submission_id,
        SubmissionStatusUpdate(
            status="validation_pending",
            reason="Awaiting validation.",
            reproduction_id=reproduction.reproduction_id,
        ),
    )
    submission = update_submission_status(
        root,
        submission.submission_id,
        SubmissionStatusUpdate(
            status="accepted",
            reason="Accepted.",
            validation_id=validation.validation_id,
        ),
    )
    contribution = calculate_contribution_for_submission(
        root,
        workspace,
        submission.submission_id,
    )
    assert contribution.eligible_for_reward
    return submission, contribution


def _setup(root_path, *, include_candidate=False, shadow_status="candidate"):
    root = root_path / "protocol"
    workspace = _workspace(root_path)
    subnet = create_subnet(
        root,
        SubnetCreate(category="access_control", exploration_ratio=0.2),
    )
    active = _node(root, "reward-active")
    _member(root, subnet, active, "active", [90] * 6)
    nodes_per_category = 1
    if include_candidate:
        candidate = _node(root, "reward-candidate")
        _member(
            root,
            subnet,
            candidate,
            shadow_status,
            [80] * 3 if shadow_status == "probation" else None,
        )
        nodes_per_category = 2
    calculated = calculate_project_routing(
        root,
        workspace,
        "project-1",
        ProjectRoutingRequest(
            categories=["access_control"],
            nodes_per_category=nodes_per_category,
            include_exploration=True,
        ),
    )
    routing = finalize_project_routing(
        root,
        workspace,
        "project-1",
        calculated.record.routing_id,
    ).record
    submissions = []
    for index, assignment in enumerate(routing.results[0].assignments, start=1):
        submissions.append(
            _accepted_submission(
                root,
                workspace,
                routing,
                assignment,
                f"finding-{index}",
            )
        )
    return root, workspace, subnet, routing, submissions


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("candidate", None),
        ("probation", Decimal("0.90")),
        ("active", Decimal("1.00")),
        ("expert", Decimal("1.10")),
        ("suspended", None),
        ("removed", None),
    ],
)
def test_membership_multipliers(status, expected):
    assert get_membership_reward_multiplier(status) == expected


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        ("0", "0.80"),
        ("0.399999", "0.80"),
        ("0.40", "0.95"),
        ("0.599999", "0.95"),
        ("0.60", "1.05"),
        ("0.799999", "1.05"),
        ("0.80", "1.15"),
        ("1", "1.15"),
    ],
)
def test_category_multipliers(score, expected):
    assert get_category_reward_multiplier(Decimal(score)) == Decimal(expected)


def test_category_pool_largest_remainder_is_exact_and_order_independent():
    first = allocate_category_pools(
        Decimal("100.000000"),
        ["reentrancy", "access_control", "oracle_manipulation"],
        None,
    )
    second = allocate_category_pools(
        Decimal("100.000000"),
        ["access_control", "oracle_manipulation", "reentrancy"],
        None,
    )
    assert first == second
    assert sum(first.values()) == Decimal("100.000000")
    assert first["access_control"] == Decimal("33.333334")
    weighted = allocate_category_pools(
        Decimal("10000.000000"),
        ["reentrancy", "access_control"],
        {"access_control": Decimal("3"), "reentrancy": Decimal("2")},
    )
    assert weighted == {
        "access_control": Decimal("6000.000000"),
        "reentrancy": Decimal("4000.000000"),
    }


def test_proportional_allocation_is_exact_and_deterministic():
    items = [
        WeightedRewardItem(
            allocation_id="b",
            node_id="node-b",
            submission_id="submission-b",
            raw_weight=Decimal("1"),
            contribution_score=Decimal("80"),
        ),
        WeightedRewardItem(
            allocation_id="a",
            node_id="node-a",
            submission_id="submission-a",
            raw_weight=Decimal("1"),
            contribution_score=Decimal("80"),
        ),
        WeightedRewardItem(
            allocation_id="c",
            node_id="node-c",
            submission_id="submission-c",
            raw_weight=Decimal("1"),
            contribution_score=Decimal("80"),
        ),
    ]
    rewards = allocate_proportional_rewards(Decimal("100.000000"), items)
    assert sum(rewards.values()) == Decimal("100.000000")
    assert rewards["a"] == Decimal("33.333334")
    assert rewards == allocate_proportional_rewards(
        Decimal("100.000000"),
        list(reversed(items)),
    )


def test_cycle_creation_calculation_finalization_and_idempotency(tmp_path):
    root, workspace, subnet, routing, submissions = _setup(tmp_path)
    request = SubnetRewardCycleCreateRequest(
        routing_id=routing.routing_id,
        total_pool_points="1000.000000",
    )
    draft = create_subnet_reward_cycle(
        root, workspace, "project-1", request
    )
    assert draft.status == SubnetRewardCycleStatus.DRAFT
    assert draft.total_undistributed_points == Decimal("1000.000000")
    assert create_subnet_reward_cycle(
        root, workspace, "project-1", request
    ).reward_cycle_id == draft.reward_cycle_id
    with pytest.raises(SubnetRewardCycleConflictError):
        create_subnet_reward_cycle(
            root,
            workspace,
            "project-1",
            SubnetRewardCycleCreateRequest(
                routing_id=routing.routing_id,
                total_pool_points="999.000000",
            ),
        )

    calculated = calculate_subnet_reward_cycle(
        root,
        workspace,
        "project-1",
        draft.reward_cycle_id,
    )
    assert calculated.status.value == "calculated"
    assert calculated.eligible_allocations == 1
    assert calculated.cycle.total_distributed_points == Decimal("1000.000000")
    assert calculated.cycle.total_undistributed_points == Decimal("0.000000")
    assert calculated.cycle.category_pools[0].allocations[0].reward_points == Decimal(
        "1000.000000"
    )
    unchanged = calculate_subnet_reward_cycle(
        root,
        workspace,
        "project-1",
        draft.reward_cycle_id,
    )
    assert unchanged.status.value == "unchanged"

    source_records = {
        "routing": deepcopy(
            load_routing_record(root, "project-1", routing.routing_id)
        ),
        "submission": deepcopy(load_submission(root, submissions[0][0].submission_id)),
        "contribution": deepcopy(
            load_contribution_score(root, submissions[0][0].submission_id)
        ),
        "validation": deepcopy(
            load_validation_decision(workspace, submissions[0][0].finding_id)
        ),
        "reproduction": deepcopy(
            load_reproduction_result(workspace, submissions[0][0].finding_id)
        ),
        "node": deepcopy(load_node(root, submissions[0][0].node_id)),
        "member": deepcopy(
            load_subnet_member(
                root,
                subnet.subnet_id,
                submissions[0][0].node_id,
            )
        ),
    }
    finalized = finalize_subnet_reward_cycle(
        root,
        workspace,
        "project-1",
        draft.reward_cycle_id,
    )
    assert finalized.status.value == "finalized"
    assert finalized.reward_events_created == 1
    assert len(list_subnet_reward_events(root)) == 1
    repeated = finalize_subnet_reward_cycle(
        root,
        workspace,
        "project-1",
        draft.reward_cycle_id,
    )
    assert repeated.status.value == "already_finalized"
    assert len(list_subnet_reward_events(root)) == 1
    with pytest.raises(SubnetRewardAlreadyFinalizedError):
        calculate_subnet_reward_cycle(
            root,
            workspace,
            "project-1",
            draft.reward_cycle_id,
        )
    assert load_routing_record(root, "project-1", routing.routing_id) == source_records["routing"]
    assert load_submission(root, submissions[0][0].submission_id) == source_records["submission"]
    assert load_contribution_score(root, submissions[0][0].submission_id) == source_records["contribution"]
    assert load_validation_decision(workspace, submissions[0][0].finding_id) == source_records["validation"]
    assert load_reproduction_result(workspace, submissions[0][0].finding_id) == source_records["reproduction"]
    assert load_node(root, submissions[0][0].node_id) == source_records["node"]
    assert load_subnet_member(root, subnet.subnet_id, submissions[0][0].node_id) == source_records["member"]


def test_candidate_shadow_is_excluded_and_pool_stays_in_category(tmp_path):
    root, workspace, _, routing, submissions = _setup(
        tmp_path,
        include_candidate=True,
    )
    draft = create_subnet_reward_cycle(
        root,
        workspace,
        "project-1",
        SubnetRewardCycleCreateRequest(
            routing_id=routing.routing_id,
            total_pool_points="100.000000",
        ),
    )
    calculated = calculate_subnet_reward_cycle(
        root,
        workspace,
        "project-1",
        draft.reward_cycle_id,
    )
    pool = calculated.cycle.category_pools[0]
    assert pool.eligible_submissions == 1
    assert pool.ineligible_submissions == 1
    assert pool.distributed_points == Decimal("100.000000")
    assert "candidate_shadow_ineligible" in {
        reason.value
        for exclusion in pool.exclusions
        for reason in exclusion.reasons
    }


def test_probation_shadow_receives_reduced_multiplier(tmp_path):
    root, workspace, _, routing, _ = _setup(
        tmp_path,
        include_candidate=True,
        shadow_status="probation",
    )
    draft = create_subnet_reward_cycle(
        root,
        workspace,
        "project-1",
        SubnetRewardCycleCreateRequest(
            routing_id=routing.routing_id,
            total_pool_points="100.000000",
        ),
    )
    calculated = calculate_subnet_reward_cycle(
        root,
        workspace,
        "project-1",
        draft.reward_cycle_id,
    )
    shadow = next(
        allocation
        for allocation in calculated.cycle.category_pools[0].allocations
        if allocation.assignment_mode.value == "shadow"
    )
    assert shadow.membership_status.value == "probation"
    assert shadow.membership_multiplier == Decimal("0.90")


def test_source_change_blocks_finalization(tmp_path):
    root, workspace, _, routing, submissions = _setup(tmp_path)
    draft = create_subnet_reward_cycle(
        root,
        workspace,
        "project-1",
        SubnetRewardCycleCreateRequest(
            routing_id=routing.routing_id,
            total_pool_points="100.000000",
        ),
    )
    calculate_subnet_reward_cycle(
        root,
        workspace,
        "project-1",
        draft.reward_cycle_id,
    )
    reproduction = load_reproduction_result(
        workspace,
        submissions[0][0].finding_id,
    )
    save_reproduction_result(
        reproduction.model_copy(update={"status": ReproductionStatus.FAILED}),
        workspace,
    )
    with pytest.raises(SubnetRewardSourceChangedError):
        finalize_subnet_reward_cycle(
            root,
            workspace,
            "project-1",
            draft.reward_cycle_id,
        )
    assert list_subnet_reward_events(root) == []


def test_storage_path_is_human_readable_and_contains_no_sensitive_data(tmp_path):
    root, workspace, _, routing, _ = _setup(tmp_path)
    cycle = create_subnet_reward_cycle(
        root,
        workspace,
        "project-1",
        SubnetRewardCycleCreateRequest(
            routing_id=routing.routing_id,
            total_pool_points="100.000000",
        ),
    )
    path = get_subnet_reward_cycle_path(
        root,
        "project-1",
        cycle.reward_cycle_id,
    )
    assert path == (
        root
        / "subnet-rewards"
        / "projects"
        / "project-1"
        / cycle.reward_cycle_id
        / "cycle.json"
    )
    text = path.read_text(encoding="utf-8").lower()
    assert text.startswith("{\n  ")
    for forbidden in ("/home/", "private_key", "wallet", "poc_file", "stdout"):
        assert forbidden not in text
    assert load_subnet_reward_cycle(
        root,
        "project-1",
        cycle.reward_cycle_id,
    ) == cycle
