import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from app.schemas.node import NodeCreate, NodeStatusChangeRequest
from app.schemas.routing import (
    ProjectRoutingRequest,
    RoutingSelectionType,
)
from app.schemas.scope import ScopeManifest
from app.schemas.subnet import (
    SubnetCreate,
    SubnetMemberRecord,
    SubnetStatusChangeRequest,
)
from app.services.category_performance_service import save_category_performance
from app.services.category_scoring_service import rebuild_category_score
from app.services.node_registry_service import change_node_status, create_node
from app.services.subnet_registry_service import (
    change_subnet_status,
    create_subnet,
    load_subnet_member,
    save_subnet_member,
)
from app.services.subnet_router_service import (
    RoutingCategoryNotInScopeError,
    RoutingIncompleteError,
    RoutingSourceChangedError,
    calculate_project_routing,
    calculate_routing_slot_targets,
    collect_node_routing_usage,
    finalize_project_routing,
    get_routing_record_path,
    is_exploration_routing_candidate,
    is_ranked_routing_candidate,
    list_project_routing_records,
    list_routing_usage_events,
    load_routing_record,
    resolve_project_routing_categories,
)
from tests.test_category_scoring_service import _performance


NOW = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)


def _workspace(root, project_id="project-1", categories=None):
    workspace = root / "workspace"
    (workspace / "scope").mkdir(parents=True)
    scope = ScopeManifest(
        project_name="Router Project",
        language="Solidity",
        framework="Foundry",
        contracts_in_scope=["src/Vault.sol"],
        attack_categories=categories or ["access_control"],
    )
    (workspace / "scope" / "parsed_scope.json").write_text(
        json.dumps(scope.model_dump(mode="json"), indent=2),
        encoding="utf-8",
    )
    (workspace / "metadata.json").write_text(
        json.dumps({"project_id": project_id, "status": "created"}),
        encoding="utf-8",
    )
    return workspace


def _node(root, name, categories=None):
    return create_node(
        root,
        NodeCreate(
            node_type="agent",
            display_name=name,
            operator_id=f"operator-{name}",
            supported_categories=categories or ["access_control"],
        ),
    )


def _member(root, subnet, node, status, scores=None):
    score = None
    if scores is not None:
        performance = save_category_performance(
            root,
            _performance(
                node_id=node.node_id,
                category=subnet.category.value,
                accepted_scores=scores,
            ),
        )
        score = rebuild_category_score(
            root, node.node_id, subnet.category
        ).record
        finalized = performance.counts.total_finalized_submissions
        accepted = performance.counts.accepted_unique_submissions
    else:
        finalized = 1
        accepted = 0
    membership_fingerprint = hashlib.sha256(
        f"{subnet.subnet_id}:{node.node_id}:{status}".encode()
    ).hexdigest()
    member = SubnetMemberRecord(
        subnet_id=subnet.subnet_id,
        node_id=node.node_id,
        category=subnet.category,
        status=status,
        category_score=score.category_score if score is not None else 0.5,
        rank=None,
        finalized_submissions=finalized,
        accepted_unique_submissions=accepted,
        exploration_assignments=0,
        last_assigned_at=None,
        joined_at=NOW,
        updated_at=NOW,
        status_reason="Router test membership.",
        category_score_id=(
            score.score_id
            if score is not None
            else f"unavailable_category_score_{node.node_id}_{subnet.category.value}"
        ),
        category_score_source_fingerprint=(
            score.source_fingerprint if score is not None else None
        ),
        performance_id=(
            score.performance_id
            if score is not None
            else f"unavailable_performance_{node.node_id}_{subnet.category.value}"
        ),
        membership_source_fingerprint=membership_fingerprint,
        last_decision_id="membership-decision-test",
        last_evaluated_at=NOW,
        status_updated_at=NOW,
        status_reason_codes=["eligible_" + status],
        administrative_lock=False,
    )
    return save_subnet_member(root, member), score


@pytest.mark.parametrize(
    ("nodes", "ratio", "include", "expected"),
    [
        (4, 0.2, False, (4, 0)),
        (1, 0.2, True, (1, 0)),
        (2, 0.2, True, (1, 1)),
        (4, 0.2, True, (3, 1)),
        (5, 0.2, True, (4, 1)),
        (10, 0.2, True, (8, 2)),
        (4, 0, True, (4, 0)),
    ],
)
def test_slot_calculation(nodes, ratio, include, expected):
    targets = calculate_routing_slot_targets(nodes, ratio, include)
    assert targets == expected
    assert sum(targets) == nodes
    assert targets[0] >= 1


def test_scope_categories_are_sorted_and_explicit_categories_are_strict(tmp_path):
    workspace = _workspace(
        tmp_path, categories=["reentrancy", "access_control"]
    )
    assert resolve_project_routing_categories(workspace, None) == [
        "access_control",
        "reentrancy",
    ]
    assert resolve_project_routing_categories(
        workspace, ["reentrancy"]
    ) == ["reentrancy"]
    with pytest.raises(RoutingCategoryNotInScopeError):
        resolve_project_routing_categories(workspace, ["governance"])


def test_ranked_and_exploration_eligibility_obey_membership_snapshots(tmp_path):
    root = tmp_path / "protocol"
    subnet = create_subnet(root, SubnetCreate(category="access_control"))
    active_node = _node(root, "active-node")
    active_member, active_score = _member(
        root, subnet, active_node, "active", [90] * 6
    )
    assert is_ranked_routing_candidate(
        active_node, subnet, active_member, active_score
    )[0]
    assert not is_exploration_routing_candidate(
        active_node, subnet, active_member, active_score
    )[0]

    candidate_node = _node(root, "candidate-node")
    candidate_member, _ = _member(
        root, subnet, candidate_node, "candidate"
    )
    assert is_exploration_routing_candidate(
        candidate_node, subnet, candidate_member, None
    )[0]
    assert not is_ranked_routing_candidate(
        candidate_node, subnet, candidate_member, None
    )[0]

    stale = active_member.model_copy(
        update={"category_score_source_fingerprint": "f" * 64}
    )
    assert not is_ranked_routing_candidate(
        active_node, subnet, stale, active_score
    )[0]


def test_routing_selects_ranked_and_shadow_deterministically(tmp_path):
    root = tmp_path / "protocol"
    workspace = _workspace(tmp_path)
    subnet = create_subnet(
        root,
        SubnetCreate(category="access_control", exploration_ratio=0.2),
    )
    nodes = [
        _node(root, "expert-node"),
        _node(root, "active-a"),
        _node(root, "active-b"),
        _node(root, "probation-node"),
        _node(root, "candidate-node"),
        _node(root, "suspended-node"),
    ]
    _member(root, subnet, nodes[0], "expert", [100] * 10)
    _member(root, subnet, nodes[1], "active", [90] * 6)
    _member(root, subnet, nodes[2], "active", [80] * 5)
    _member(root, subnet, nodes[3], "probation", [80] * 3)
    _member(root, subnet, nodes[4], "candidate")
    _member(root, subnet, nodes[5], "suspended", [100] * 10)

    request = ProjectRoutingRequest(nodes_per_category=4)
    first = calculate_project_routing(
        root, workspace, "project-1", request
    )
    result = first.record.results[0]
    assert first.status.value == "created"
    assert (result.ranked_target, result.exploration_target) == (3, 1)
    assert result.complete
    assert [item.assignment_mode.value for item in result.assignments] == [
        "production",
        "production",
        "production",
        "shadow",
    ]
    assert result.assignments[0].node_id == nodes[0].node_id
    assert result.assignments[-1].node_id == nodes[3].node_id
    assert nodes[5].node_id not in {
        assignment.node_id for assignment in result.assignments
    }
    assert all(item.qualification_reference is None for item in result.assignments)
    assert all(load_subnet_member(root, subnet.subnet_id, node.node_id).rank is None for node in nodes)

    unchanged = calculate_project_routing(
        root, workspace, "project-1", request
    )
    assert unchanged.status.value == "unchanged"
    assert unchanged.record.routing_id == first.record.routing_id


def test_partial_routing_and_missing_subnet_are_explicit(tmp_path):
    root = tmp_path / "protocol"
    workspace = _workspace(
        tmp_path, categories=["access_control", "reentrancy"]
    )
    subnet = create_subnet(root, SubnetCreate(category="access_control"))
    node = _node(root, "only-node")
    _member(root, subnet, node, "active", [80] * 5)
    response = calculate_project_routing(
        root,
        workspace,
        "project-1",
        ProjectRoutingRequest(nodes_per_category=4, allow_partial=True),
    )
    assert response.record.partial_categories == 1
    assert response.record.failed_categories == 1
    assert "no_subnet" in [
        reason.value for reason in response.record.results[1].shortage_reasons
    ]
    with pytest.raises(RoutingIncompleteError):
        calculate_project_routing(
            root,
            workspace,
            "project-1",
            ProjectRoutingRequest(
                nodes_per_category=4,
                allow_partial=False,
            ),
        )


def test_finalization_usage_is_idempotent_and_does_not_change_membership(tmp_path):
    root = tmp_path / "protocol"
    workspace = _workspace(tmp_path)
    subnet = create_subnet(root, SubnetCreate(category="access_control"))
    active = _node(root, "finalize-active")
    candidate = _node(root, "finalize-candidate")
    _member(root, subnet, active, "active", [90] * 6)
    _member(root, subnet, candidate, "candidate")
    before_members = {
        node.node_id: deepcopy(load_subnet_member(root, subnet.subnet_id, node.node_id))
        for node in (active, candidate)
    }
    calculated = calculate_project_routing(
        root,
        workspace,
        "project-1",
        ProjectRoutingRequest(nodes_per_category=2),
    )
    assert list_routing_usage_events(root) == []
    finalized = finalize_project_routing(
        root,
        workspace,
        "project-1",
        calculated.record.routing_id,
    )
    assert finalized.status.value == "finalized"
    assert finalized.usage_events_created == 2
    repeat = finalize_project_routing(
        root,
        workspace,
        "project-1",
        calculated.record.routing_id,
    )
    assert repeat.status.value == "already_finalized"
    assert len(list_routing_usage_events(root)) == 2
    assert collect_node_routing_usage(
        root, candidate.node_id, subnet.subnet_id, "access_control"
    ).exploration_assignments == 1
    for node in (active, candidate):
        assert load_subnet_member(
            root, subnet.subnet_id, node.node_id
        ) == before_members[node.node_id]


def test_changed_sources_block_finalization(tmp_path):
    root = tmp_path / "protocol"
    workspace = _workspace(tmp_path)
    subnet = create_subnet(root, SubnetCreate(category="access_control"))
    node = _node(root, "stale-node")
    member, _ = _member(root, subnet, node, "active", [80] * 5)
    calculated = calculate_project_routing(
        root,
        workspace,
        "project-1",
        ProjectRoutingRequest(nodes_per_category=1),
    )
    save_subnet_member(
        root,
        member.model_copy(
            update={"membership_source_fingerprint": "f" * 64}
        ),
    )
    with pytest.raises(RoutingSourceChangedError):
        finalize_project_routing(
            root,
            workspace,
            "project-1",
            calculated.record.routing_id,
        )
    assert list_routing_usage_events(root) == []


def test_storage_paths_and_lists_are_deterministic_and_safe(tmp_path):
    root = tmp_path / "protocol"
    workspace = _workspace(tmp_path)
    subnet = create_subnet(root, SubnetCreate(category="access_control"))
    node = _node(root, "storage-node")
    _member(root, subnet, node, "active", [80] * 5)
    response = calculate_project_routing(
        root,
        workspace,
        "project-1",
        ProjectRoutingRequest(nodes_per_category=1),
    )
    path = get_routing_record_path(
        root, "project-1", response.record.routing_id
    )
    assert path == (
        root
        / "routing"
        / "projects"
        / "project-1"
        / response.record.routing_id
        / "routing.json"
    )
    assert path.read_text().startswith("{\n  ")
    text = path.read_text().lower()
    assert "/home/" not in text
    assert "private_key" not in text
    assert "benchmark_answers" not in text
    assert load_routing_record(
        root, "project-1", response.record.routing_id
    ) == response.record
    assert list_project_routing_records(root, "project-1") == [response.record]
