from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.agents.access_control_agent import AccessControlAgent
from app.agents.reentrancy_agent import ReentrancyAgent
from app.schemas.agent_execution import (
    AgentExecutionRecord,
    AgentExecutionStatus,
    AgentExecutorType,
)
from app.schemas.finding import FindingCategory
from app.schemas.node import NodeRecord, NodeStatistics, NodeStatus, NodeType
from app.schemas.routing import (
    RoutingAssignment,
    RoutingAssignmentMode,
    RoutingCandidateSnapshot,
    RoutingSelectionType,
)
from app.schemas.subnet import SubnetMemberStatus
from app.services.local_agent_executor import (
    AgentTaskContext,
    LOCAL_ACCESS_NODE_ID,
    LOCAL_REENTRANCY_NODE_ID,
    LocalAgentBinding,
    LocalAgentCapabilityError,
    LocalAgentExecutor,
    LocalAgentNotRegisteredError,
    LocalAgentRegistry,
)


NOW = datetime(2026, 8, 18, tzinfo=timezone.utc)


def _node(node_id, operator_id, category):
    return NodeRecord(
        node_id=node_id,
        node_type=NodeType.AGENT,
        operator_id=operator_id,
        display_name=f"Node {category.value}",
        public_key=None,
        supported_categories=[category.value],
        description=None,
        status=NodeStatus.ACTIVE,
        reputation_score=0.5,
        statistics=NodeStatistics(),
        status_reason=None,
        created_at=NOW,
        updated_at=NOW,
        status_updated_at=NOW,
    )


def _assignment(node, category):
    subnet_id = f"subnet_{category.value}"
    return RoutingAssignment(
        assignment_id=f"assignment_{node.node_id}",
        routing_id="routing_1",
        project_id="project_1",
        subnet_id=subnet_id,
        category=category,
        node_id=node.node_id,
        selection_type=RoutingSelectionType.EXPLORATION,
        assignment_mode=RoutingAssignmentMode.SHADOW,
        membership_status=SubnetMemberStatus.CANDIDATE,
        category_score=0.5,
        experience_confidence=0,
        position=1,
        selection_reasons=["Simulator candidate selected."],
        candidate_snapshot=RoutingCandidateSnapshot(
            node_id=node.node_id,
            subnet_id=subnet_id,
            category=category,
            node_status=NodeStatus.ACTIVE,
            membership_status=SubnetMemberStatus.CANDIDATE,
            category_score=0.5,
            experience_confidence=0,
            finalized_submissions=0,
            accepted_unique_submissions=0,
            membership_source_fingerprint="a" * 64,
            exploration_assignments_before=0,
        ),
        created_at=NOW,
    )


def _workspace(tmp_path):
    workspace = tmp_path / "project"
    repo = workspace / "repo" / "src"
    repo.mkdir(parents=True)
    (repo / "Vulnerable.sol").write_text(
        """
        contract Vulnerable {
            mapping(address => uint256) balances;
            function setTreasury(address value) external { treasury = value; }
            function withdraw() external {
                payable(msg.sender).call{value: balances[msg.sender]}(\"\");
                balances[msg.sender] = 0;
            }
        }
        """,
        encoding="utf-8",
    )
    return workspace


@pytest.mark.parametrize(
    ("node_id", "operator_id", "category", "agent_class", "title"),
    [
        (
            LOCAL_ACCESS_NODE_ID,
            "operator_local_access",
            FindingCategory.ACCESS_CONTROL,
            AccessControlAgent,
            "Missing access control",
        ),
        (
            LOCAL_REENTRANCY_NODE_ID,
            "operator_local_reentrancy",
            FindingCategory.REENTRANCY,
            ReentrancyAgent,
            "Potential reentrancy",
        ),
    ],
)
def test_registry_and_executor_run_real_scoped_agents(
    tmp_path, node_id, operator_id, category, agent_class, title
):
    workspace = _workspace(tmp_path)
    node = _node(node_id, operator_id, category)
    assignment = _assignment(node, category)
    registry = LocalAgentRegistry()
    binding, agent = registry.resolve(node, category)
    assert isinstance(agent, agent_class)
    assert binding.agent_version.endswith("_v1")
    result = LocalAgentExecutor(registry).execute(
        context=AgentTaskContext(
            audit_run_id="audit_run_1",
            project_id="project_1",
            routing_id="routing_1",
            assignment=assignment,
            node=node,
            project_workspace=workspace,
            workspace_ref="project:project_1",
            scope={"contracts_in_scope": ["src/Vulnerable.sol"]},
        ),
        agent_execution_id="agent_execution_1",
    )
    assert result.success is True
    assert len(result.finding_candidates) == 1
    assert result.finding_candidates[0].title.startswith(title)
    assert result.finding_candidates[0].category == category
    assert result.finding_candidates[0].status.value == "candidate"


def test_executor_scope_and_category_enforcement(tmp_path):
    workspace = _workspace(tmp_path)
    node = _node(
        LOCAL_ACCESS_NODE_ID,
        "operator_local_access",
        FindingCategory.ACCESS_CONTROL,
    )
    assignment = _assignment(node, FindingCategory.ACCESS_CONTROL)
    out_of_scope = LocalAgentExecutor().execute(
        context=AgentTaskContext(
            audit_run_id="audit_run_1",
            project_id="project_1",
            routing_id="routing_1",
            assignment=assignment,
            node=node,
            project_workspace=workspace,
            workspace_ref="project:project_1",
            scope={"contracts_in_scope": ["src/Safe.sol"]},
        ),
        agent_execution_id="agent_execution_1",
    )
    assert out_of_scope.finding_candidates == []

    wrong = _node(
        LOCAL_ACCESS_NODE_ID,
        "operator_local_access",
        FindingCategory.REENTRANCY,
    )
    with pytest.raises(LocalAgentCapabilityError):
        LocalAgentRegistry().resolve(wrong, FindingCategory.REENTRANCY)
    with pytest.raises(LocalAgentNotRegisteredError):
        LocalAgentRegistry().resolve(
            _node("unknown_node", "operator_x", FindingCategory.ACCESS_CONTROL),
            FindingCategory.ACCESS_CONTROL,
        )


def test_registry_rejects_misconfigured_agent_mapping():
    registry = LocalAgentRegistry(
        [
            LocalAgentBinding(
                node_id="bad_node",
                category=FindingCategory.ACCESS_CONTROL,
                agent_type="BadRuntime",
                agent_version="bad_v1",
                factory=ReentrancyAgent,
            )
        ]
    )
    with pytest.raises(LocalAgentCapabilityError):
        registry.resolve(
            _node("bad_node", "operator_bad", FindingCategory.ACCESS_CONTROL),
            FindingCategory.ACCESS_CONTROL,
        )


def _record(status, **overrides):
    values = {
        "agent_execution_id": "agent_execution_1",
        "audit_run_id": "audit_run_1",
        "project_id": "project_1",
        "routing_id": "routing_1",
        "routing_assignment_id": "assignment_1",
        "node_id": "node_1",
        "operator_id": "operator_1",
        "category": "access_control",
        "executor_type": AgentExecutorType.LOCAL,
        "agent_type": "AccessControlAgent",
        "agent_version": "access_control_static_v1",
        "status": status,
        "source_fingerprint": "b" * 64,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return AgentExecutionRecord(**values)


def test_agent_execution_record_lifecycle_and_ref_ordering():
    assert _record(AgentExecutionStatus.PENDING).attempt_count == 0
    assert _record(
        AgentExecutionStatus.RUNNING, started_at=NOW, attempt_count=1
    ).status == AgentExecutionStatus.RUNNING
    completed = _record(
        AgentExecutionStatus.COMPLETED,
        started_at=NOW,
        completed_at=NOW + timedelta(milliseconds=2),
        duration_ms=2,
        attempt_count=1,
        finding_ids=["finding_1", "finding_2"],
        finding_count=2,
        submission_ids=["submission_1"],
        submission_count=1,
        updated_at=NOW + timedelta(milliseconds=2),
    )
    assert completed.finding_count == 2
    failed = _record(
        AgentExecutionStatus.FAILED,
        started_at=NOW,
        completed_at=NOW,
        duration_ms=0,
        attempt_count=1,
        error_code="LOCAL_AGENT_EXECUTION_FAILED",
        error_message="Local execution failed safely.",
    )
    assert failed.error_code == "LOCAL_AGENT_EXECUTION_FAILED"
    with pytest.raises(ValidationError):
        _record(
            AgentExecutionStatus.COMPLETED,
            started_at=NOW,
            completed_at=NOW,
            duration_ms=0,
            attempt_count=1,
            finding_ids=["finding_2", "finding_1"],
            finding_count=2,
        )
    with pytest.raises(ValidationError):
        _record(
            AgentExecutionStatus.FAILED,
            started_at=NOW,
            completed_at=NOW,
            duration_ms=0,
            attempt_count=1,
            error_code="FAILED",
            error_message="trace in /tmp/private.py",
        )
