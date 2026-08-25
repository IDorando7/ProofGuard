from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.paths import protocol_data_root
from app.schemas.audit_run import (
    AuditEventListResponse,
    AuditEventType,
    AuditRun,
    AuditRunActionRequest,
    AuditRunCreateRequest,
    AuditRunListResponse,
    AuditStage,
)
from app.schemas.agent_execution import (
    AgentExecutionListResponse,
    AgentExecutionRecord,
)
from app.services.agent_execution_service import (
    AgentExecutionNotFoundError,
    AgentExecutionServiceError,
    list_agent_executions,
    require_agent_execution,
)
from app.services.local_agent_executor import (
    LocalAgentRegistry,
    get_local_agent_registry,
)
from app.services.audit_day2_handlers import (
    AgentExecutionStageHandler,
    ProjectPreparationStageHandler,
    RoutingStageHandler,
)
from app.services.audit_orchestrator import (
    AuditOrchestrator,
    InvalidAuditTransitionError,
)
from app.services.audit_run_service import (
    AuditRunConflictError,
    AuditRunNotFoundError,
    AuditRunServiceError,
    AuditRunStorageError,
    InvalidAuditRunIdentifierError,
    create_audit_run,
    list_audit_events,
    list_audit_runs,
    require_audit_run,
)
from app.services.project_service import (
    get_project_or_404,
    load_project_scope,
    project_workspace,
)
from app.utils.protocol_serialization import protocol_fingerprint


router = APIRouter(prefix="/projects/{project_id}/audit-runs", tags=["audit-runs"])


def _raise_audit_run_error(exc: Exception) -> None:
    if isinstance(exc, AuditRunNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, (AuditRunConflictError, InvalidAuditTransitionError)):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, InvalidAuditRunIdentifierError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, AuditRunStorageError):
        raise HTTPException(status_code=500, detail="AuditRun storage invariant failed") from exc
    if isinstance(exc, AuditRunServiceError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, AgentExecutionNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, AgentExecutionServiceError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise exc


@router.post("", response_model=AuditRun, status_code=201)
def create_project_audit_run(
    project_id: str,
    payload: AuditRunCreateRequest,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> AuditRun:
    get_project_or_404(db, project_id)
    scope_fingerprint = protocol_fingerprint(load_project_scope(project_id))
    try:
        return create_audit_run(
            root,
            project_id,
            payload.execution_mode,
            scope_fingerprint,
        )
    except Exception as exc:
        _raise_audit_run_error(exc)


@router.get("", response_model=AuditRunListResponse)
def list_project_audit_runs(
    project_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> AuditRunListResponse:
    get_project_or_404(db, project_id)
    try:
        runs = list_audit_runs(root, project_id)
    except Exception as exc:
        _raise_audit_run_error(exc)
    return AuditRunListResponse(
        project_id=project_id,
        total=len(runs),
        audit_runs=runs,
    )


@router.get("/{audit_run_id}", response_model=AuditRun)
def get_project_audit_run(
    project_id: str,
    audit_run_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> AuditRun:
    get_project_or_404(db, project_id)
    try:
        return require_audit_run(root, project_id, audit_run_id)
    except Exception as exc:
        _raise_audit_run_error(exc)


@router.post("/{audit_run_id}/start", response_model=AuditRun)
def start_project_audit_run(
    project_id: str,
    audit_run_id: str,
    payload: AuditRunActionRequest | None = Body(default=None),
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
    registry: LocalAgentRegistry = Depends(get_local_agent_registry),
) -> AuditRun:
    del payload
    project = get_project_or_404(db, project_id)
    try:
        run = require_audit_run(root, project_id, audit_run_id)
        if run.execution_mode.value != "local_simulator":
            raise AuditRunConflictError(
                "Remote network execution is not implemented"
            )
        if project.status != "ready":
            # Preserve the Day 1 lifecycle operation for projects whose existing
            # prepare worker has not completed. No business stage is fabricated.
            return AuditOrchestrator(root).start(project_id, audit_run_id)
        workspace = project_workspace(project_id)
        return AuditOrchestrator(
            root,
            stage_handlers={
                AuditStage.PREPARING: ProjectPreparationStageHandler(workspace),
                AuditStage.ROUTING: RoutingStageHandler(workspace),
                AuditStage.EXECUTING_AGENTS: AgentExecutionStageHandler(
                    workspace,
                    registry=registry,
                ),
            },
            stop_after_stage=AuditStage.EXECUTING_AGENTS,
        ).run(project_id, audit_run_id)
    except Exception as exc:
        _raise_audit_run_error(exc)


@router.get(
    "/{audit_run_id}/agent-executions",
    response_model=AgentExecutionListResponse,
)
def list_project_agent_executions(
    project_id: str,
    audit_run_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> AgentExecutionListResponse:
    get_project_or_404(db, project_id)
    try:
        records = list_agent_executions(root, project_id, audit_run_id)
        return AgentExecutionListResponse(
            audit_run_id=audit_run_id,
            total=len(records),
            agent_executions=records,
        )
    except Exception as exc:
        _raise_audit_run_error(exc)


@router.get(
    "/{audit_run_id}/agent-executions/{agent_execution_id}",
    response_model=AgentExecutionRecord,
)
def get_project_agent_execution(
    project_id: str,
    audit_run_id: str,
    agent_execution_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> AgentExecutionRecord:
    get_project_or_404(db, project_id)
    try:
        require_audit_run(root, project_id, audit_run_id)
        return require_agent_execution(
            root, project_id, audit_run_id, agent_execution_id
        )
    except Exception as exc:
        _raise_audit_run_error(exc)


@router.get("/{audit_run_id}/events", response_model=AuditEventListResponse)
def get_project_audit_run_events(
    project_id: str,
    audit_run_id: str,
    after_sequence: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
    stage: AuditStage | None = Query(default=None),
    event_type: AuditEventType | None = Query(default=None),
    node_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> AuditEventListResponse:
    get_project_or_404(db, project_id)
    try:
        return list_audit_events(
            root,
            project_id,
            audit_run_id,
            after_sequence=after_sequence,
            limit=limit,
            stage=stage,
            event_type=event_type,
            node_id=node_id,
        )
    except Exception as exc:
        _raise_audit_run_error(exc)
