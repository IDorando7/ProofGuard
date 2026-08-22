import json

from app.agents.base import BaseAgent
from app.schemas.audit_run import (
    AuditEventType,
    AuditExecutionMode,
    AuditRunStatus,
    AuditStage,
    AuditStageStatus,
)
from app.schemas.finding import FindingCategory
from app.schemas.routing import ProjectRoutingRequest
from app.schemas.scope import ScopeManifest
from app.services.agent_execution_service import (
    list_agent_execution_attributions,
    list_agent_executions,
)
from app.services.audit_day2_handlers import (
    AgentExecutionStageHandler,
    ProjectPreparationStageHandler,
    RoutingStageHandler,
)
from app.services.audit_orchestrator import AuditOrchestrator, AuditStageContext
from app.services.audit_run_service import (
    create_audit_run,
    list_audit_events,
)
from app.services.finding_service import list_project_findings
from app.services.local_agent_executor import (
    LOCAL_ACCESS_NODE_ID,
    LocalAgentBinding,
    LocalAgentExecutor,
    LocalAgentRegistry,
)
from app.services.local_simulator_service import (
    bootstrap_local_simulator_routing_capacity,
)
from app.services.scope_service import write_scope_files
from app.services.submission_service import list_project_submissions
from app.services.subnet_router_service import load_routing_record
from app.utils.protocol_serialization import protocol_fingerprint


def _project(tmp_path):
    project_id = "project_day2"
    workspace = tmp_path / "workspace"
    repo = workspace / "repo" / "src"
    repo.mkdir(parents=True)
    source = """
    contract Vulnerable {
        address treasury;
        mapping(address => uint256) balances;
        function setTreasury(address value) external { treasury = value; }
        function withdraw() external {
            payable(msg.sender).call{value: balances[msg.sender]}(\"\");
            balances[msg.sender] = 0;
        }
    }
    """
    (repo / "Vulnerable.sol").write_text(source, encoding="utf-8")
    scope = ScopeManifest(
        project_name="Day 2 fixture",
        language="Solidity",
        framework="Foundry",
        contracts_in_scope=["src/Vulnerable.sol"],
        attack_categories=["access_control", "reentrancy"],
    )
    write_scope_files(workspace, b"project_name: fixture\n", scope)
    (workspace / "findings").mkdir()
    (workspace / "metadata.json").write_text(
        json.dumps({"project_id": project_id, "status": "ready"}),
        encoding="utf-8",
    )
    return project_id, workspace, scope


def _orchestrator(protocol_root, workspace, *, execution_handler=None):
    return AuditOrchestrator(
        protocol_root,
        stage_handlers={
            AuditStage.PREPARING: ProjectPreparationStageHandler(workspace),
            AuditStage.ROUTING: RoutingStageHandler(
                workspace,
                ProjectRoutingRequest(nodes_per_category=5, allow_partial=True),
            ),
            AuditStage.EXECUTING_AGENTS: execution_handler
            or AgentExecutionStageHandler(workspace),
        },
        stop_after_stage=AuditStage.EXECUTING_AGENTS,
    )


def test_day2_happy_path_is_real_observable_and_idempotent(tmp_path):
    protocol_root = tmp_path / "protocol"
    project_id, workspace, scope = _project(tmp_path)
    members = bootstrap_local_simulator_routing_capacity(protocol_root)
    assert len(members) == 2
    assert bootstrap_local_simulator_routing_capacity(protocol_root) == members
    run = create_audit_run(
        protocol_root,
        project_id,
        AuditExecutionMode.LOCAL_SIMULATOR,
        protocol_fingerprint(scope.model_dump(mode="json")),
    )

    completed = _orchestrator(protocol_root, workspace).run(
        project_id, run.audit_run_id
    )
    states = {state.stage: state for state in completed.stage_states}
    assert completed.status == AuditRunStatus.RUNNING
    assert completed.current_stage == AuditStage.EXECUTING_AGENTS
    assert states[AuditStage.PREPARING].status == AuditStageStatus.COMPLETED
    assert states[AuditStage.ROUTING].status == AuditStageStatus.COMPLETED
    assert states[AuditStage.EXECUTING_AGENTS].status == AuditStageStatus.COMPLETED
    assert states[AuditStage.REPRODUCING].status == AuditStageStatus.PENDING
    assert completed.routing_id is not None

    routing = load_routing_record(
        protocol_root, project_id, completed.routing_id
    )
    assert routing is not None and routing.status.value == "finalized"
    assignments = [
        assignment
        for result in routing.results
        for assignment in result.assignments
    ]
    assert len(assignments) == 2
    assert {item.node_id for item in assignments} == {
        "node_local_access",
        "node_local_reentrancy",
    }
    assert all(not result.complete for result in routing.results)

    executions = list_agent_executions(
        protocol_root, project_id, run.audit_run_id
    )
    assert len(executions) == 2
    assert all(item.status.value == "COMPLETED" for item in executions)
    assert {item.agent_type for item in executions} == {
        "AccessControlAgent",
        "ReentrancyAgent",
    }
    assert all(item.duration_ms is not None for item in executions)
    findings = list_project_findings(workspace)
    submissions = list_project_submissions(protocol_root, project_id)
    assert len(findings) == 2
    assert len(submissions) == 2
    assert {item.finding_id for item in findings} == {
        finding_id for item in executions for finding_id in item.finding_ids
    }
    assert {item.submission_id for item in submissions} == {
        submission_id for item in executions for submission_id in item.submission_ids
    }
    assert all(item.status.value == "submitted" for item in submissions)
    assert all(item.reproduction_id is None for item in submissions)
    assert all(item.validation_id is None for item in submissions)
    assert all(item.metadata["audit_run_id"] == run.audit_run_id for item in submissions)
    finding_links = list_agent_execution_attributions(
        protocol_root, "finding", findings[0].finding_id
    )
    assert len(finding_links) == 1
    assert finding_links[0]["audit_run_id"] == run.audit_run_id
    assert finding_links[0]["routing_id"] == completed.routing_id
    assert finding_links[0]["node_id"]
    assert finding_links[0]["operator_id"]
    submission_links = list_agent_execution_attributions(
        protocol_root, "submission", submissions[0].submission_id
    )
    assert len(submission_links) == 1
    assert submission_links[0]["agent_execution_id"] in {
        item.agent_execution_id for item in executions
    }

    events = list_audit_events(
        protocol_root, project_id, run.audit_run_id, limit=200
    ).events
    types = [item.event_type for item in events]
    assert types.count(AuditEventType.ROUTING_NODE_SELECTED) == 2
    assert types.count(AuditEventType.AGENT_EXECUTION_STARTED) == 2
    assert types.count(AuditEventType.AGENT_EXECUTION_COMPLETED) == 2
    assert types.count(AuditEventType.FINDING_CREATED) == 2
    assert types.count(AuditEventType.SUBMISSION_CREATED) == 2
    assert [event.sequence_number for event in events] == list(
        range(1, len(events) + 1)
    )
    assert all("/tmp/" not in event.model_dump_json() for event in events)
    selected = next(
        item for item in events if item.event_type == AuditEventType.ROUTING_NODE_SELECTED
    )
    assert selected.node_id and selected.operator_id and selected.category
    assert selected.metadata["selection_reasons"]
    routing_completed = next(
        item
        for item in events
        if item.event_type == AuditEventType.STAGE_COMPLETED
        and item.stage == AuditStage.ROUTING
    )
    assert routing_completed.metadata["stage_metadata"]["capacity_shortfalls"]

    snapshot = (
        [item.agent_execution_id for item in executions],
        [item.finding_id for item in findings],
        [item.submission_id for item in submissions],
        completed.latest_event_sequence,
    )
    repeated = _orchestrator(protocol_root, workspace).run(
        project_id, run.audit_run_id
    )
    repeated_snapshot = (
        [
            item.agent_execution_id
            for item in list_agent_executions(
                protocol_root, project_id, run.audit_run_id
            )
        ],
        [item.finding_id for item in list_project_findings(workspace)],
        [
            item.submission_id
            for item in list_project_submissions(protocol_root, project_id)
        ],
        repeated.latest_event_sequence,
    )
    assert repeated_snapshot == snapshot

    assert not (protocol_root / "reproduction-results").exists()
    assert not (protocol_root / "validation-decisions").exists()
    assert not (protocol_root / "reward-events").exists()


class _ThrowingAgent(BaseAgent):
    name = "throwing_agent"
    category = FindingCategory.ACCESS_CONTROL

    def run(self, project_id, repo_path, scope):
        raise RuntimeError(f"private failure at {repo_path}")


def test_failed_local_execution_is_safe_and_fails_required_stage(tmp_path):
    protocol_root = tmp_path / "protocol"
    project_id, workspace, scope = _project(tmp_path)
    bootstrap_local_simulator_routing_capacity(protocol_root)
    registry = LocalAgentRegistry(
        [
            LocalAgentBinding(
                node_id=LOCAL_ACCESS_NODE_ID,
                category=FindingCategory.ACCESS_CONTROL,
                agent_type="ThrowingAgent",
                agent_version="throwing_v1",
                factory=_ThrowingAgent,
            )
        ]
    )
    run = create_audit_run(
        protocol_root,
        project_id,
        AuditExecutionMode.LOCAL_SIMULATOR,
        protocol_fingerprint(scope.model_dump(mode="json")),
    )
    failed = _orchestrator(
        protocol_root,
        workspace,
        execution_handler=AgentExecutionStageHandler(
            workspace,
            registry=registry,
            executor=LocalAgentExecutor(registry),
        ),
    ).run(project_id, run.audit_run_id)
    assert failed.status == AuditRunStatus.FAILED
    assert failed.failure_stage == AuditStage.EXECUTING_AGENTS
    executions = list_agent_executions(
        protocol_root, project_id, run.audit_run_id
    )
    assert len(executions) == 1
    assert executions[0].status.value == "FAILED"
    assert executions[0].finding_ids == []
    assert "/tmp/" not in executions[0].error_message
    events = list_audit_events(
        protocol_root, project_id, run.audit_run_id, limit=200
    ).events
    failure = next(
        item
        for item in events
        if item.event_type == AuditEventType.AGENT_EXECUTION_FAILED
    )
    assert failure.metadata["error_code"] == "LOCAL_AGENT_EXECUTION_FAILED"
    assert "/tmp/" not in failure.model_dump_json()


def test_completed_execution_records_reconcile_after_stage_interruption(tmp_path):
    protocol_root = tmp_path / "protocol"
    project_id, workspace, scope = _project(tmp_path)
    bootstrap_local_simulator_routing_capacity(protocol_root)
    run = create_audit_run(
        protocol_root,
        project_id,
        AuditExecutionMode.LOCAL_SIMULATOR,
        protocol_fingerprint(scope.model_dump(mode="json")),
    )
    routing_only = AuditOrchestrator(
        protocol_root,
        stage_handlers={
            AuditStage.PREPARING: ProjectPreparationStageHandler(workspace),
            AuditStage.ROUTING: RoutingStageHandler(workspace),
        },
        stop_after_stage=AuditStage.ROUTING,
    )
    routed = routing_only.run(project_id, run.audit_run_id)
    running = routing_only.start_stage(
        project_id, run.audit_run_id, AuditStage.EXECUTING_AGENTS
    )
    handler = AgentExecutionStageHandler(workspace)
    handler.execute(
        AuditStageContext(
            audit_run_id=run.audit_run_id,
            project_id=project_id,
            protocol_data_root=protocol_root,
            report_progress=lambda current, total, summary: routing_only.update_stage_progress(
                project_id,
                run.audit_run_id,
                AuditStage.EXECUTING_AGENTS,
                progress_current=current,
                progress_total=total,
                summary=summary,
            ),
        )
    )
    before = list_agent_executions(protocol_root, project_id, run.audit_run_id)
    assert len(before) == 2
    assert all(item.attempt_count == 1 for item in before)
    assert all(item.status.value == "COMPLETED" for item in before)
    assert running.current_stage == AuditStage.EXECUTING_AGENTS

    reconciled = _orchestrator(protocol_root, workspace).run(
        project_id, run.audit_run_id
    )
    after = list_agent_executions(protocol_root, project_id, run.audit_run_id)
    assert after == before
    assert reconciled.status == AuditRunStatus.RUNNING
    assert next(
        state
        for state in reconciled.stage_states
        if state.stage == AuditStage.EXECUTING_AGENTS
    ).status == AuditStageStatus.COMPLETED
    events = list_audit_events(
        protocol_root, project_id, run.audit_run_id, limit=200
    ).events
    assert sum(
        item.event_type == AuditEventType.AGENT_EXECUTION_COMPLETED
        for item in events
    ) == 2
    assert sum(
        item.event_type == AuditEventType.STAGE_COMPLETED
        and item.stage == AuditStage.EXECUTING_AGENTS
        for item in events
    ) == 1
