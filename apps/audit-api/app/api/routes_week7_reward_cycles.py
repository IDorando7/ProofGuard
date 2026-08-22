from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.paths import protocol_data_root
from app.schemas.week7_reward_cycle import (
    Week7RewardHistorySubject,
    Week7RewardProcessingStatus,
    Week7TaskRewardCycle,
    Week7TaskRewardCycleActionRequest,
    Week7TaskRewardCycleCreateRequest,
    Week7TaskRewardCycleListResponse,
    Week7TaskRewardCycleResponse,
    Week7TaskRewardCycleVerification,
    Week7TaskRewardEventListResponse,
    Week7TaskRewardHistorySummary,
)
from app.services.project_service import get_project_or_404, project_workspace
from app.services.week7_reward_cycle_service import (
    Week7RewardCycleConflictError,
    Week7RewardCycleConservationError,
    Week7RewardCycleNotFoundError,
    Week7RewardCycleSourceChangedError,
    Week7RewardCycleStateError,
    Week7RewardCycleStorageError,
    Week7RewardEventConflictError,
    calculate_week7_task_reward_cycle,
    create_week7_task_reward_cycle,
    finalize_week7_task_reward_cycle,
    get_week7_reward_history,
    list_project_week7_reward_cycles,
    list_week7_reward_events_response,
    load_week7_reward_cycle,
    load_week7_reward_cycle_by_routing,
    verify_week7_task_reward_cycle,
)


router = APIRouter(tags=["week7-task-reward-cycles"])


def _raise_protocol_error(exc: Exception) -> None:
    if isinstance(exc, Week7RewardCycleNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(
        exc,
        (
            Week7RewardCycleConflictError,
            Week7RewardCycleSourceChangedError,
            Week7RewardCycleStateError,
            Week7RewardEventConflictError,
        ),
    ):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(
        exc, (Week7RewardCycleStorageError, Week7RewardCycleConservationError)
    ):
        raise HTTPException(
            status_code=500,
            detail="Week 7 reward-cycle storage or conservation invariant failed",
        ) from exc
    raise exc


@router.post(
    "/projects/{project_id}/routing/{routing_id}/task-reward-cycles",
    response_model=Week7TaskRewardCycleResponse,
    status_code=201,
)
def create_project_week7_reward_cycle(
    project_id: str,
    routing_id: str,
    payload: Week7TaskRewardCycleCreateRequest,
    response: Response,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> Week7TaskRewardCycleResponse:
    get_project_or_404(db, project_id)
    try:
        cycle, created = create_week7_task_reward_cycle(
            root,
            project_workspace(project_id),
            project_id,
            routing_id,
            payload,
        )
    except Exception as exc:
        _raise_protocol_error(exc)
    if not created:
        response.status_code = 200
    return Week7TaskRewardCycleResponse(
        processing_status=(
            Week7RewardProcessingStatus.CREATED
            if created
            else Week7RewardProcessingStatus.UNCHANGED
        ),
        cycle=cycle,
        message=(
            "Draft Week 7 client-task reward cycle created; no reward was calculated or paid."
            if created
            else "Existing Week 7 reward cycle reused; no duplicate cycle was created."
        ),
    )


@router.post(
    "/projects/{project_id}/task-reward-cycles/{reward_cycle_id}/calculate",
    response_model=Week7TaskRewardCycleResponse,
)
def calculate_project_week7_reward_cycle(
    project_id: str,
    reward_cycle_id: str,
    payload: Week7TaskRewardCycleActionRequest | None = Body(default=None),
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> Week7TaskRewardCycleResponse:
    del payload
    get_project_or_404(db, project_id)
    try:
        cycle, status = calculate_week7_task_reward_cycle(
            root, project_id, reward_cycle_id
        )
    except Exception as exc:
        _raise_protocol_error(exc)
    return Week7TaskRewardCycleResponse(
        processing_status=status,
        cycle=cycle,
        message=(
            "Week 7 calculation sources were unchanged; no RewardEvent was created."
            if status == Week7RewardProcessingStatus.UNCHANGED
            else "Week 7 Day 4 and Day 5 snapshots were integrated; no RewardEvent was created."
        ),
    )


@router.post(
    "/projects/{project_id}/task-reward-cycles/{reward_cycle_id}/finalize",
    response_model=Week7TaskRewardCycleResponse,
)
def finalize_project_week7_reward_cycle(
    project_id: str,
    reward_cycle_id: str,
    payload: Week7TaskRewardCycleActionRequest | None = Body(default=None),
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> Week7TaskRewardCycleResponse:
    del payload
    get_project_or_404(db, project_id)
    try:
        return finalize_week7_task_reward_cycle(root, project_id, reward_cycle_id)
    except Exception as exc:
        _raise_protocol_error(exc)


@router.get(
    "/projects/{project_id}/task-reward-cycles/{reward_cycle_id}",
    response_model=Week7TaskRewardCycle,
)
def get_project_week7_reward_cycle(
    project_id: str,
    reward_cycle_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> Week7TaskRewardCycle:
    get_project_or_404(db, project_id)
    try:
        cycle = load_week7_reward_cycle(root, project_id, reward_cycle_id)
    except Exception as exc:
        _raise_protocol_error(exc)
    if cycle is None:
        raise HTTPException(status_code=404, detail="Week 7 reward cycle not found")
    return cycle


@router.get(
    "/projects/{project_id}/task-reward-cycles",
    response_model=Week7TaskRewardCycleListResponse,
)
def list_project_week7_reward_cycle_records(
    project_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> Week7TaskRewardCycleListResponse:
    get_project_or_404(db, project_id)
    try:
        return list_project_week7_reward_cycles(root, project_id)
    except Exception as exc:
        _raise_protocol_error(exc)


@router.get(
    "/projects/{project_id}/routing/{routing_id}/task-reward-cycle",
    response_model=Week7TaskRewardCycle,
)
def get_project_week7_reward_cycle_by_routing(
    project_id: str,
    routing_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> Week7TaskRewardCycle:
    get_project_or_404(db, project_id)
    try:
        cycle = load_week7_reward_cycle_by_routing(root, project_id, routing_id)
    except Exception as exc:
        _raise_protocol_error(exc)
    if cycle is None:
        raise HTTPException(status_code=404, detail="Week 7 reward cycle not found")
    return cycle


@router.get(
    "/projects/{project_id}/task-reward-cycles/{reward_cycle_id}/events",
    response_model=Week7TaskRewardEventListResponse,
)
def list_project_week7_cycle_events(
    project_id: str,
    reward_cycle_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> Week7TaskRewardEventListResponse:
    get_project_or_404(db, project_id)
    cycle = load_week7_reward_cycle(root, project_id, reward_cycle_id)
    if cycle is None:
        raise HTTPException(status_code=404, detail="Week 7 reward cycle not found")
    return list_week7_reward_events_response(
        root, reward_cycle_id=reward_cycle_id, project_id=project_id
    )


@router.get(
    "/projects/{project_id}/task-reward-cycles/{reward_cycle_id}/verify",
    response_model=Week7TaskRewardCycleVerification,
)
def verify_project_week7_reward_cycle(
    project_id: str,
    reward_cycle_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> Week7TaskRewardCycleVerification:
    get_project_or_404(db, project_id)
    try:
        return verify_week7_task_reward_cycle(root, project_id, reward_cycle_id)
    except Exception as exc:
        _raise_protocol_error(exc)


def _history(
    root: Path, subject_type: Week7RewardHistorySubject, subject_id: str
) -> Week7TaskRewardHistorySummary:
    try:
        return get_week7_reward_history(root, subject_type, subject_id)
    except Exception as exc:
        _raise_protocol_error(exc)


@router.get(
    "/operators/{operator_id}/task-rewards",
    response_model=Week7TaskRewardHistorySummary,
)
def get_operator_week7_rewards(
    operator_id: str, root: Path = Depends(protocol_data_root)
) -> Week7TaskRewardHistorySummary:
    return _history(root, Week7RewardHistorySubject.OPERATOR, operator_id)


@router.get(
    "/nodes/{node_id}/task-rewards",
    response_model=Week7TaskRewardHistorySummary,
)
def get_node_week7_rewards(
    node_id: str, root: Path = Depends(protocol_data_root)
) -> Week7TaskRewardHistorySummary:
    return _history(root, Week7RewardHistorySubject.NODE, node_id)


@router.get(
    "/submissions/{submission_id}/task-rewards",
    response_model=Week7TaskRewardHistorySummary,
)
def get_submission_week7_rewards(
    submission_id: str, root: Path = Depends(protocol_data_root)
) -> Week7TaskRewardHistorySummary:
    return _history(root, Week7RewardHistorySubject.SUBMISSION, submission_id)


@router.get(
    "/finding-clusters/{finding_cluster_id}/task-rewards",
    response_model=Week7TaskRewardHistorySummary,
)
def get_finding_cluster_week7_rewards(
    finding_cluster_id: str, root: Path = Depends(protocol_data_root)
) -> Week7TaskRewardHistorySummary:
    return _history(
        root, Week7RewardHistorySubject.FINDING_CLUSTER, finding_cluster_id
    )
