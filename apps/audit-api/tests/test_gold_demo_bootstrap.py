from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from app.api.routes_demo import bootstrap_demo_network
from app.core.config import Settings
from app.core.paths import protocol_data_root
from app.main import app
from app.schemas.node import NodeRecord, NodeStatistics, NodeStatus, NodeType
from app.schemas.subnet import SubnetMemberStatus
from app.services.demo_bootstrap_service import (
    DemoBootstrapDisabledError,
    bootstrap_gold_demo_network,
)
from app.services.local_agent_executor import (
    LocalAgentRegistry,
    get_local_agent_registry,
)
from app.services.node_registry_service import load_node, save_node
from app.services.subnet_registry_service import (
    list_subnet_members,
    load_subnet,
)
from app.services.subnet_router_service import calculate_routing_slot_targets
from tests.test_project_creation import _create_project, _zip_bytes


NOW = datetime(2026, 8, 24, tzinfo=timezone.utc)
CATEGORIES = ("access_control", "reentrancy")


def _seed_specialists(root):
    nodes = []
    for category, label in (
        ("access_control", "access"),
        ("reentrancy", "reentrancy"),
    ):
        for index in range(1, 5):
            nodes.append(
                save_node(
                    root,
                    NodeRecord(
                        node_id=f"node_demo_{label}_{index:02d}",
                        node_type=NodeType.AGENT,
                        display_name=f"Demo {label} specialist {index:02d}",
                        operator_id=f"demo-agent-{label}-op-{index:02d}",
                        public_key=None,
                        supported_categories=[category],
                        description="Gold Demo specialist fixture.",
                        status=NodeStatus.ACTIVE,
                        reputation_score=0.5,
                        statistics=NodeStatistics(),
                        status_reason="Gold Demo fixture registered.",
                        created_at=NOW,
                        updated_at=NOW,
                        status_updated_at=NOW,
                    ),
                )
            )
    return nodes


def test_demo_mode_disabled_has_no_bootstrap_side_effects(tmp_path):
    root = tmp_path / "protocol"
    registry = LocalAgentRegistry([])
    with pytest.raises(DemoBootstrapDisabledError):
        bootstrap_gold_demo_network(root, registry, demo_mode=False)
    assert registry.node_ids() == ()
    assert not root.exists()

    with pytest.raises(HTTPException) as exc_info:
        bootstrap_demo_network(
            root=root,
            settings=Settings(demo_mode=False),
            registry=registry,
        )
    assert exc_info.value.status_code == 404
    if not Settings().demo_mode:
        assert "/demo/bootstrap" not in app.openapi()["paths"]


def test_demo_bootstrap_registers_runtimes_and_derives_mature_membership(tmp_path):
    root = tmp_path / "protocol"
    seeded = _seed_specialists(root)
    registry = LocalAgentRegistry([])

    result = bootstrap_gold_demo_network(root, registry, demo_mode=True)
    repeated = bootstrap_gold_demo_network(root, registry, demo_mode=True)

    assert result.historical_submission_count == repeated.historical_submission_count == 36
    assert set(registry.node_ids()) == {node.node_id for node in seeded}
    assert calculate_routing_slot_targets(4, 0.20, True) == (3, 1)

    for category_result in result.categories:
        assert category_result.active_members == 3
        assert category_result.candidate_members >= 1
        assert category_result.probation_members == 0
        assert [node.category_score for node in category_result.nodes] == [
            0.8283,
            0.7784,
            0.7185,
            0.0,
        ]
        assert [node.membership_status for node in category_result.nodes] == [
            SubnetMemberStatus.ACTIVE,
            SubnetMemberStatus.ACTIVE,
            SubnetMemberStatus.ACTIVE,
            SubnetMemberStatus.CANDIDATE,
        ]
        expected_runtime = (
            "AccessControlAgent"
            if category_result.category.value == "access_control"
            else "ReentrancyAgent"
        )
        assert {node.runtime_type for node in category_result.nodes} == {
            expected_runtime
        }

        subnet = load_subnet(root, f"subnet_{category_result.category.value}")
        assert subnet is not None
        assert subnet.minimum_category_score == 0.60
        assert subnet.minimum_finalized_submissions == 5
        for member in list_subnet_members(root, subnet.subnet_id):
            if member.status in {
                SubnetMemberStatus.CANDIDATE,
                SubnetMemberStatus.PROBATION,
                SubnetMemberStatus.ACTIVE,
                SubnetMemberStatus.EXPERT,
            }:
                node = load_node(root, member.node_id)
                assert node is not None
                binding, agent = registry.resolve(node, category_result.category)
                assert binding.node_id == node.node_id
                assert agent.category == category_result.category


def test_gold_demo_routing_and_post_start_execute_all_assignments(client):
    root = app.dependency_overrides[protocol_data_root]()
    _seed_specialists(root)
    registry = LocalAgentRegistry([])
    bootstrap_gold_demo_network(root, registry, demo_mode=True)
    app.dependency_overrides[get_local_agent_registry] = lambda: registry

    vulnerable = """
    contract Vault {
        address treasury;
        mapping(address => uint256) balances;
        function setTreasury(address value) external { treasury = value; }
        function withdraw() external {
            payable(msg.sender).call{value: balances[msg.sender]}(\"\");
            balances[msg.sender] = 0;
        }
    }
    """
    project = _create_project(
        client,
        _zip_bytes(
            {
                "src/Vault.sol": vulnerable,
                "src/OracleRouter.sol": "contract OracleRouter {}",
            }
        ),
    ).json()
    project_id = project["project_id"]
    prepared = client.post(f"/projects/{project_id}/prepare")
    assert prepared.status_code == 200
    assert client.get(f"/projects/{project_id}/status").json()["status"] == "ready"

    created = client.post(
        f"/projects/{project_id}/audit-runs",
        json={"execution_mode": "local_simulator"},
    )
    assert created.status_code == 201
    audit_run_id = created.json()["audit_run_id"]
    started = client.post(
        f"/projects/{project_id}/audit-runs/{audit_run_id}/start",
        json={},
    )
    assert started.status_code == 200
    body = started.json()
    assert body["failure_stage"] is None
    assert body["error_message"] is None
    assert body["routing_id"]

    routing = client.get(
        f"/projects/{project_id}/routing/{body['routing_id']}"
    ).json()
    assert len(routing["results"]) == 2
    for category in CATEGORIES:
        result = next(
            item for item in routing["results"] if item["category"] == category
        )
        assert result["requested_assignments"] == 4
        assert result["ranked_target"] == 3
        assert result["exploration_target"] == 1
        assert result["ranked_selected"] == 3
        assert result["exploration_selected"] == 1
        assert result["complete"] is True
        assert "no_ranked_members" not in result["shortage_reasons"]
        for assignment in result["assignments"]:
            node = load_node(root, assignment["node_id"])
            assert node is not None
            registry.resolve(node, category)

    executions = client.get(
        f"/projects/{project_id}/audit-runs/{audit_run_id}/agent-executions"
    ).json()
    assert executions["total"] == 8
    assert all(item["status"] == "COMPLETED" for item in executions["agent_executions"])
    assert sum(item["finding_count"] for item in executions["agent_executions"]) > 0
    assert sum(item["submission_count"] for item in executions["agent_executions"]) > 0

    submissions = client.get(f"/projects/{project_id}/submissions").json()
    assert len(submissions) > 0
