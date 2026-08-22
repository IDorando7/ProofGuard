from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.schemas.agent_execution import (
    AgentExecutionRecord,
    AgentExecutionStatus,
    AgentExecutorType,
    agent_execution_source_fingerprint,
)
from app.schemas.audit_run import (
    AuditEntityRef,
    AuditEventLevel,
    AuditEventType,
    AuditStage,
    StageResult,
)
from app.schemas.routing import ProjectRoutingRequest, RoutingAssignment
from app.services.agent_execution_service import (
    create_agent_execution,
    index_agent_execution_entities,
    list_agent_executions,
    logical_agent_execution_id,
    save_agent_execution,
)
from app.services.audit_orchestrator import AuditStageContext, AuditStageHandler
from app.services.audit_run_service import (
    append_audit_event,
    set_audit_run_routing_id,
)
from app.services.local_agent_executor import (
    AgentTaskContext,
    LocalAgentExecutor,
    LocalAgentRegistry,
)
from app.services.node_finding_ingestion_service import NodeFindingIngestionService
from app.services.node_registry_service import ensure_node_can_participate
from app.services.scope_service import read_parsed_scope
from app.services.subnet_router_service import (
    calculate_project_routing,
    finalize_project_routing,
)
from app.utils.protocol_serialization import protocol_fingerprint


class ProjectPreparationStageHandler:
    """Verifies the real preparation invariants without rerunning the worker."""

    def __init__(self, project_workspace: Path) -> None:
        self.project_workspace = project_workspace

    def execute(self, context: AuditStageContext) -> StageResult:
        repo = self.project_workspace / "repo"
        if not repo.is_dir():
            raise ValueError("Prepared project repository is missing")
        scope = read_parsed_scope(self.project_workspace)
        missing: list[str] = []
        for item in scope.contracts_in_scope:
            relative = Path(item)
            if relative.is_absolute() or ".." in relative.parts:
                missing.append(item)
                continue
            direct = repo / relative
            if direct.is_file():
                continue
            matches = list(repo.rglob(relative.name))
            if not any(match.as_posix().endswith(relative.as_posix()) for match in matches):
                missing.append(item)
        if missing:
            raise ValueError("Prepared project is missing scoped contracts")
        return StageResult(
            stage=AuditStage.PREPARING,
            success=True,
            summary=f"Prepared repository and {len(scope.contracts_in_scope)} scoped contracts verified.",
            progress_current=len(scope.contracts_in_scope),
            progress_total=len(scope.contracts_in_scope),
            metadata={
                "workspace_ref": f"project:{context.project_id}",
                "scoped_contract_count": len(scope.contracts_in_scope),
            },
        )


class RoutingStageHandler:
    def __init__(
        self,
        project_workspace: Path,
        request: ProjectRoutingRequest | None = None,
    ) -> None:
        self.project_workspace = project_workspace
        self.request = request or ProjectRoutingRequest()

    def execute(self, context: AuditStageContext) -> StageResult:
        calculation = calculate_project_routing(
            context.protocol_data_root,
            self.project_workspace,
            context.project_id,
            self.request,
        )
        finalization = finalize_project_routing(
            context.protocol_data_root,
            self.project_workspace,
            context.project_id,
            calculation.record.routing_id,
        )
        record = finalization.record
        set_audit_run_routing_id(
            context.protocol_data_root,
            context.project_id,
            context.audit_run_id,
            record.routing_id,
        )
        assignments = _ordered_assignments(record.results)
        for assignment in assignments:
            node = ensure_node_can_participate(
                context.protocol_data_root,
                assignment.node_id,
                assignment.category.value,
            )
            append_audit_event(
                context.protocol_data_root,
                context.project_id,
                context.audit_run_id,
                event_type=AuditEventType.ROUTING_NODE_SELECTED,
                stage=AuditStage.ROUTING,
                event_level=AuditEventLevel.INFO,
                message="Routing assignment selected a node.",
                entity_type="routing_assignment",
                entity_id=assignment.assignment_id,
                node_id=assignment.node_id,
                operator_id=node.operator_id,
                category=assignment.category.value,
                semantic_key=f"routing:{record.routing_id}:assignment:{assignment.assignment_id}:selected",
                metadata={
                    "routing_id": record.routing_id,
                    "assignment_id": assignment.assignment_id,
                    "assignment_mode": assignment.assignment_mode.value,
                    "selection_type": assignment.selection_type.value,
                    "membership": assignment.membership_status.value,
                    "category_score": assignment.category_score,
                    "position": assignment.position,
                    "selection_reasons": assignment.selection_reasons,
                },
            )
        category_total = len(record.results)
        context.report_progress(
            category_total,
            category_total,
            f"Routed {category_total} categories into {len(assignments)} assignments.",
        )
        requested = sum(result.requested_assignments for result in record.results)
        shortfalls = [
            {
                "category": result.category.value,
                "requested": result.requested_assignments,
                "assigned": result.total_selected,
                "capacity_shortfall": result.requested_assignments - result.total_selected,
                "reasons": [reason.value for reason in result.shortage_reasons],
            }
            for result in record.results
            if not result.complete
        ]
        return StageResult(
            stage=AuditStage.ROUTING,
            success=True,
            summary=f"Routing finalized with {len(assignments)} of {requested} requested assignments.",
            progress_current=category_total,
            progress_total=category_total,
            entity_refs=[
                AuditEntityRef(entity_type="routing", entity_id=record.routing_id)
            ],
            metadata={
                "routing_id": record.routing_id,
                "categories": [item.category.value for item in record.results],
                "requested_assignments": requested,
                "created_assignments": len(assignments),
                "ranked_count": record.production_assignments,
                "exploration_count": record.shadow_assignments,
                "capacity_shortfalls": shortfalls,
            },
        )


class AgentExecutionStageHandler:
    def __init__(
        self,
        project_workspace: Path,
        *,
        registry: LocalAgentRegistry | None = None,
        executor: LocalAgentExecutor | None = None,
    ) -> None:
        self.project_workspace = project_workspace
        self.registry = registry or LocalAgentRegistry()
        self.executor = executor or LocalAgentExecutor(self.registry)

    def execute(self, context: AuditStageContext) -> StageResult:
        from app.services.audit_run_service import require_audit_run
        from app.services.subnet_router_service import load_routing_record

        run = require_audit_run(
            context.protocol_data_root, context.project_id, context.audit_run_id
        )
        if run.routing_id is None:
            raise ValueError("AuditRun has no finalized routing reference")
        routing = load_routing_record(
            context.protocol_data_root, context.project_id, run.routing_id
        )
        if routing is None or routing.status.value != "finalized":
            raise ValueError("AuditRun routing is not finalized")
        assignments = _ordered_assignments(routing.results)
        total = len(assignments)
        completed = 0
        current_state = next(
            state for state in run.stage_states if state.stage == AuditStage.EXECUTING_AGENTS
        )
        previously_reported = current_state.progress_current
        refs: list[AuditEntityRef] = []
        scope_model = read_parsed_scope(self.project_workspace)
        scope = scope_model.model_dump(mode="json")
        scope_fingerprint = protocol_fingerprint(scope)
        for assignment in assignments:
            execution = self._execute_assignment(
                context,
                assignment,
                scope,
                scope_fingerprint,
            )
            refs.append(
                AuditEntityRef(
                    entity_type="agent_execution",
                    entity_id=execution.agent_execution_id,
                )
            )
            if execution.status != AgentExecutionStatus.COMPLETED:
                raise ValueError("A required local agent execution failed")
            completed += 1
            if completed >= previously_reported:
                context.report_progress(
                    completed,
                    total,
                    f"{completed} of {total} node assignments completed.",
                )
        if total == 0:
            context.report_progress(0, 0, "No executable routing assignments were selected.")
        records = list_agent_executions(
            context.protocol_data_root, context.project_id, context.audit_run_id
        )
        return StageResult(
            stage=AuditStage.EXECUTING_AGENTS,
            success=True,
            summary=f"Completed {completed} local agent executions.",
            progress_current=completed,
            progress_total=total,
            entity_refs=refs,
            metadata={
                "selected_node_count": total,
                "agent_execution_count": len(records),
                "completed_agent_execution_count": sum(
                    item.status == AgentExecutionStatus.COMPLETED for item in records
                ),
                "finding_count": sum(item.finding_count for item in records),
                "submission_count": sum(item.submission_count for item in records),
            },
        )

    def _execute_assignment(
        self,
        context: AuditStageContext,
        assignment: RoutingAssignment,
        scope: dict,
        scope_fingerprint: str,
    ) -> AgentExecutionRecord:
        node = ensure_node_can_participate(
            context.protocol_data_root,
            assignment.node_id,
            assignment.category.value,
        )
        binding, _agent = self.registry.resolve(node, assignment.category)
        execution_id = logical_agent_execution_id(
            context.audit_run_id, assignment.assignment_id
        )
        fingerprint = agent_execution_source_fingerprint(
            {
                "audit_run_id": context.audit_run_id,
                "project_id": context.project_id,
                "routing_id": assignment.routing_id,
                "assignment_id": assignment.assignment_id,
                "node_id": node.node_id,
                "operator_id": node.operator_id,
                "category": assignment.category.value,
                "executor_type": AgentExecutorType.LOCAL.value,
                "agent_type": binding.agent_type,
                "agent_version": binding.agent_version,
                "scope_fingerprint": scope_fingerprint,
            }
        )
        now = datetime.now(timezone.utc)
        pending = AgentExecutionRecord(
            agent_execution_id=execution_id,
            audit_run_id=context.audit_run_id,
            project_id=context.project_id,
            routing_id=assignment.routing_id,
            routing_assignment_id=assignment.assignment_id,
            node_id=node.node_id,
            operator_id=node.operator_id,
            category=assignment.category,
            executor_type=AgentExecutorType.LOCAL,
            agent_type=binding.agent_type,
            agent_version=binding.agent_version,
            status=AgentExecutionStatus.PENDING,
            source_fingerprint=fingerprint,
            created_at=now,
            updated_at=now,
        )
        existing, _created = create_agent_execution(
            context.protocol_data_root, pending
        )
        if existing.status == AgentExecutionStatus.COMPLETED:
            index_agent_execution_entities(context.protocol_data_root, existing)
            self._emit_completed_event(context, existing)
            return existing
        if existing.status == AgentExecutionStatus.FAILED:
            return existing
        started_at = datetime.now(timezone.utc)
        running = AgentExecutionRecord.model_validate(
            {
                **existing.model_dump(),
                "status": AgentExecutionStatus.RUNNING,
                "attempt_count": existing.attempt_count + 1,
                "started_at": started_at,
                "completed_at": None,
                "duration_ms": None,
                "error_code": None,
                "error_message": None,
                "updated_at": started_at,
            }
        )
        save_agent_execution(context.protocol_data_root, running)
        append_audit_event(
            context.protocol_data_root,
            context.project_id,
            context.audit_run_id,
            event_type=AuditEventType.AGENT_EXECUTION_STARTED,
            stage=AuditStage.EXECUTING_AGENTS,
            message="Local node agent execution started.",
            entity_type="agent_execution",
            entity_id=execution_id,
            node_id=node.node_id,
            operator_id=node.operator_id,
            category=assignment.category.value,
            semantic_key=f"execution:{execution_id}:started:attempt:{running.attempt_count}",
            metadata={
                "execution_id": execution_id,
                "assignment_id": assignment.assignment_id,
                "executor_type": AgentExecutorType.LOCAL.value,
                "agent_type": binding.agent_type,
                "agent_version": binding.agent_version,
                "attempt_count": running.attempt_count,
            },
        )
        try:
            result = self.executor.execute(
                context=AgentTaskContext(
                    audit_run_id=context.audit_run_id,
                    project_id=context.project_id,
                    routing_id=assignment.routing_id,
                    assignment=assignment,
                    node=node,
                    project_workspace=self.project_workspace,
                    workspace_ref=f"project:{context.project_id}",
                    scope=scope,
                ),
                agent_execution_id=execution_id,
            )
            ingestion = NodeFindingIngestionService(
                context.protocol_data_root, self.project_workspace
            ).ingest(running, result.finding_candidates)
            for item in ingestion.items:
                append_audit_event(
                    context.protocol_data_root,
                    context.project_id,
                    context.audit_run_id,
                    event_type=AuditEventType.FINDING_CREATED,
                    stage=AuditStage.EXECUTING_AGENTS,
                    message="Agent candidate finding is available.",
                    entity_type="finding",
                    entity_id=item.finding.finding_id,
                    node_id=node.node_id,
                    operator_id=node.operator_id,
                    category=assignment.category.value,
                    semantic_key=f"execution:{execution_id}:finding:{item.finding.finding_id}",
                    metadata={
                        "finding_id": item.finding.finding_id,
                        "execution_id": execution_id,
                        "reported_severity": item.finding.severity.value,
                        "affected_contracts": sorted(item.finding.contracts),
                        "affected_functions": sorted(item.finding.functions),
                    },
                )
                append_audit_event(
                    context.protocol_data_root,
                    context.project_id,
                    context.audit_run_id,
                    event_type=AuditEventType.SUBMISSION_CREATED,
                    stage=AuditStage.EXECUTING_AGENTS,
                    message="Protocol submission is available for an agent finding.",
                    entity_type="submission",
                    entity_id=item.submission.submission_id,
                    node_id=node.node_id,
                    operator_id=node.operator_id,
                    category=assignment.category.value,
                    semantic_key=f"execution:{execution_id}:submission:{item.submission.submission_id}",
                    metadata={
                        "submission_id": item.submission.submission_id,
                        "finding_id": item.submission.finding_id,
                        "execution_id": execution_id,
                        "submission_status": item.submission.status.value,
                        "finding_hash": item.submission.finding_hash,
                    },
                )
            finished_at = datetime.now(timezone.utc)
            completed = AgentExecutionRecord.model_validate(
                {
                    **running.model_dump(),
                    "status": AgentExecutionStatus.COMPLETED,
                    "completed_at": finished_at,
                    "duration_ms": max(
                        0, int((finished_at - started_at).total_seconds() * 1000)
                    ),
                    "finding_ids": ingestion.finding_ids,
                    "submission_ids": ingestion.submission_ids,
                    "finding_count": len(ingestion.finding_ids),
                    "submission_count": len(ingestion.submission_ids),
                    "updated_at": finished_at,
                }
            )
            save_agent_execution(context.protocol_data_root, completed)
            index_agent_execution_entities(context.protocol_data_root, completed)
            self._emit_completed_event(context, completed)
            return completed
        except Exception:
            finished_at = datetime.now(timezone.utc)
            failed = AgentExecutionRecord.model_validate(
                {
                    **running.model_dump(),
                    "status": AgentExecutionStatus.FAILED,
                    "completed_at": finished_at,
                    "duration_ms": max(
                        0, int((finished_at - started_at).total_seconds() * 1000)
                    ),
                    "finding_ids": [],
                    "submission_ids": [],
                    "finding_count": 0,
                    "submission_count": 0,
                    "error_code": "LOCAL_AGENT_EXECUTION_FAILED",
                    "error_message": "Local agent execution or finding ingestion failed.",
                    "updated_at": finished_at,
                }
            )
            save_agent_execution(context.protocol_data_root, failed)
            append_audit_event(
                context.protocol_data_root,
                context.project_id,
                context.audit_run_id,
                event_type=AuditEventType.AGENT_EXECUTION_FAILED,
                stage=AuditStage.EXECUTING_AGENTS,
                event_level=AuditEventLevel.ERROR,
                message="Local node agent execution failed.",
                entity_type="agent_execution",
                entity_id=execution_id,
                node_id=node.node_id,
                operator_id=node.operator_id,
                category=assignment.category.value,
                semantic_key=f"execution:{execution_id}:failed:attempt:{running.attempt_count}",
                metadata={
                    "execution_id": execution_id,
                    "error_code": failed.error_code,
                    "error_message": failed.error_message,
                },
            )
            return failed

    @staticmethod
    def _emit_completed_event(
        context: AuditStageContext, execution: AgentExecutionRecord
    ) -> None:
        append_audit_event(
            context.protocol_data_root,
            context.project_id,
            context.audit_run_id,
            event_type=AuditEventType.AGENT_EXECUTION_COMPLETED,
            stage=AuditStage.EXECUTING_AGENTS,
            message="Local node agent execution completed.",
            entity_type="agent_execution",
            entity_id=execution.agent_execution_id,
            node_id=execution.node_id,
            operator_id=execution.operator_id,
            category=execution.category.value,
            semantic_key=f"execution:{execution.agent_execution_id}:completed",
            metadata={
                "execution_id": execution.agent_execution_id,
                "finding_count": execution.finding_count,
                "submission_count": execution.submission_count,
                "duration_ms": execution.duration_ms,
            },
        )


def _ordered_assignments(results) -> list[RoutingAssignment]:
    assignments = [assignment for result in results for assignment in result.assignments]
    return sorted(
        assignments,
        key=lambda item: (
            item.category.value,
            item.assignment_mode.value,
            item.node_id,
            item.assignment_id,
        ),
    )
