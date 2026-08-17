from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.paths import protocol_data_root
from app.schemas.task_finding_reward import (
    TaskFindingCalculationProcessingStatus,
    TaskFindingRewardCalculation,
    TaskFindingRewardCalculationListResponse,
    TaskFindingRewardCalculationRequest,
    TaskFindingRewardCalculationResponse,
)
from app.services.project_service import get_project_or_404
from app.services.task_finding_reward_service import (
    TaskFindingRewardConflictError,
    TaskFindingRewardLinkageError,
    TaskFindingRewardNotFoundError,
    TaskFindingRewardStateError,
    TaskFindingRewardStorageError,
    calculate_task_finding_rewards,
    list_task_finding_calculations,
    load_latest_task_finding_calculation,
    load_task_finding_calculation,
)


router = APIRouter(tags=["task-finding-rewards"])


@router.post(
    "/projects/{project_id}/routing/{routing_id}/task-rewards/findings/calculate",
    response_model=TaskFindingRewardCalculationResponse,
    status_code=201,
)
def calculate_project_finding_rewards(
    project_id: str,
    routing_id: str,
    payload: TaskFindingRewardCalculationRequest,
    response: Response,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> TaskFindingRewardCalculationResponse:
    get_project_or_404(db, project_id)
    try:
        calculation, status = calculate_task_finding_rewards(
            root, project_id, routing_id, payload
        )
    except TaskFindingRewardNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (
        TaskFindingRewardLinkageError,
        TaskFindingRewardStateError,
        TaskFindingRewardConflictError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TaskFindingRewardStorageError as exc:
        raise HTTPException(
            status_code=500, detail="Finding reward calculation storage is malformed"
        ) from exc
    if status == TaskFindingCalculationProcessingStatus.UNCHANGED:
        response.status_code = 200
    return TaskFindingRewardCalculationResponse(
        processing_status=status,
        calculation=calculation,
        message=(
            "Existing FindingCluster valuation reused; no operator reward was distributed."
            if status == TaskFindingCalculationProcessingStatus.UNCHANGED
            else "FindingCluster values allocated from the miner pool; no operator reward was distributed."
        ),
    )


@router.get(
    "/projects/{project_id}/routing/{routing_id}/task-rewards/findings/latest",
    response_model=TaskFindingRewardCalculation,
)
def get_latest_project_finding_rewards(
    project_id: str,
    routing_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> TaskFindingRewardCalculation:
    get_project_or_404(db, project_id)
    try:
        calculation = load_latest_task_finding_calculation(
            root, project_id, routing_id
        )
    except TaskFindingRewardStorageError as exc:
        raise HTTPException(
            status_code=500, detail="Finding reward calculation storage is malformed"
        ) from exc
    if calculation is None:
        raise HTTPException(status_code=404, detail="Finding reward calculation not found")
    return calculation


@router.get(
    "/projects/{project_id}/routing/{routing_id}/task-rewards/findings",
    response_model=TaskFindingRewardCalculationListResponse,
)
def list_project_finding_rewards(
    project_id: str,
    routing_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> TaskFindingRewardCalculationListResponse:
    get_project_or_404(db, project_id)
    try:
        return list_task_finding_calculations(root, project_id, routing_id)
    except TaskFindingRewardStorageError as exc:
        raise HTTPException(
            status_code=500, detail="Finding reward calculation storage is malformed"
        ) from exc


@router.get(
    "/projects/{project_id}/routing/{routing_id}/task-rewards/findings/{calculation_id}",
    response_model=TaskFindingRewardCalculation,
)
def get_project_finding_rewards(
    project_id: str,
    routing_id: str,
    calculation_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> TaskFindingRewardCalculation:
    get_project_or_404(db, project_id)
    try:
        calculation = load_task_finding_calculation(
            root, project_id, routing_id, calculation_id
        )
    except TaskFindingRewardStorageError as exc:
        raise HTTPException(
            status_code=500, detail="Finding reward calculation storage is malformed"
        ) from exc
    if calculation is None:
        raise HTTPException(status_code=404, detail="Finding reward calculation not found")
    return calculation
