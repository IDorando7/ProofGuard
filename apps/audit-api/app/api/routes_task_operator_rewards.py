from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.paths import protocol_data_root
from app.schemas.task_operator_reward import (
    TaskOperatorProcessingStatus,
    TaskOperatorRewardCalculation,
    TaskOperatorRewardCalculationListResponse,
    TaskOperatorRewardCalculationRequest,
    TaskOperatorRewardCalculationResponse,
)
from app.services.project_service import get_project_or_404
from app.services.task_operator_reward_service import (
    TaskOperatorRewardConflictError,
    TaskOperatorRewardLinkageError,
    TaskOperatorRewardNotFoundError,
    TaskOperatorRewardStateError,
    TaskOperatorRewardStorageError,
    calculate_task_operator_rewards,
    list_task_operator_calculations,
    load_latest_task_operator_calculation,
    load_task_operator_calculation,
)


router = APIRouter(tags=["task-operator-rewards"])


@router.post(
    "/projects/{project_id}/routing/{routing_id}/task-rewards/operators/calculate",
    response_model=TaskOperatorRewardCalculationResponse,
    status_code=201,
)
def calculate_project_operator_rewards(
    project_id: str,
    routing_id: str,
    payload: TaskOperatorRewardCalculationRequest,
    response: Response,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> TaskOperatorRewardCalculationResponse:
    get_project_or_404(db, project_id)
    try:
        calculation, status = calculate_task_operator_rewards(
            root, project_id, routing_id, payload
        )
    except TaskOperatorRewardNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (
        TaskOperatorRewardLinkageError,
        TaskOperatorRewardStateError,
        TaskOperatorRewardConflictError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TaskOperatorRewardStorageError as exc:
        raise HTTPException(
            status_code=500, detail="Operator reward preview storage is malformed"
        ) from exc
    if status == TaskOperatorProcessingStatus.UNCHANGED:
        response.status_code = 200
    return TaskOperatorRewardCalculationResponse(
        processing_status=status,
        calculation=calculation,
        message=(
            "Existing operator reward preview reused; no RewardEvent was created."
            if status == TaskOperatorProcessingStatus.UNCHANGED
            else "Operator rewards calculated as a non-final preview; no RewardEvent was created."
        ),
    )


@router.get(
    "/projects/{project_id}/routing/{routing_id}/task-rewards/operators/latest",
    response_model=TaskOperatorRewardCalculation,
)
def get_latest_project_operator_rewards(
    project_id: str,
    routing_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> TaskOperatorRewardCalculation:
    get_project_or_404(db, project_id)
    try:
        calculation = load_latest_task_operator_calculation(
            root, project_id, routing_id
        )
    except TaskOperatorRewardStorageError as exc:
        raise HTTPException(
            status_code=500, detail="Operator reward preview storage is malformed"
        ) from exc
    if calculation is None:
        raise HTTPException(status_code=404, detail="Operator reward preview not found")
    return calculation


@router.get(
    "/projects/{project_id}/routing/{routing_id}/task-rewards/operators",
    response_model=TaskOperatorRewardCalculationListResponse,
)
def list_project_operator_rewards(
    project_id: str,
    routing_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> TaskOperatorRewardCalculationListResponse:
    get_project_or_404(db, project_id)
    try:
        return list_task_operator_calculations(root, project_id, routing_id)
    except TaskOperatorRewardStorageError as exc:
        raise HTTPException(
            status_code=500, detail="Operator reward preview storage is malformed"
        ) from exc


@router.get(
    "/projects/{project_id}/routing/{routing_id}/task-rewards/operators/{calculation_id}",
    response_model=TaskOperatorRewardCalculation,
)
def get_project_operator_rewards(
    project_id: str,
    routing_id: str,
    calculation_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> TaskOperatorRewardCalculation:
    get_project_or_404(db, project_id)
    try:
        calculation = load_task_operator_calculation(
            root, project_id, routing_id, calculation_id
        )
    except TaskOperatorRewardStorageError as exc:
        raise HTTPException(
            status_code=500, detail="Operator reward preview storage is malformed"
        ) from exc
    if calculation is None:
        raise HTTPException(status_code=404, detail="Operator reward preview not found")
    return calculation
