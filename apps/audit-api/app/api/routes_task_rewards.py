from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.paths import protocol_data_root
from app.schemas.task_reward import (
    TaskRewardBudget,
    TaskRewardBudgetCreateRequest,
    TaskRewardBudgetCreateResponse,
    TaskRewardBudgetFinalizationResponse,
    TaskRewardBudgetFinalizeRequest,
    TaskRewardBudgetProcessingStatus,
)
from app.services.project_service import get_project_or_404, project_workspace
from app.services.task_reward_budget_service import (
    InvalidTaskRewardIdentifierError,
    TaskRewardBudgetAlreadyFinalizedError,
    TaskRewardBudgetConflictError,
    TaskRewardBudgetConservationError,
    TaskRewardBudgetNotFoundError,
    TaskRewardBudgetStorageError,
    TaskRewardProjectMismatchError,
    TaskRewardProjectNotFoundError,
    TaskRewardRoutingNotFinalizedError,
    TaskRewardRoutingNotFoundError,
    create_task_reward_budget,
    finalize_task_reward_budget,
    list_project_task_reward_budgets,
    load_task_reward_budget,
    load_task_reward_budget_by_routing,
)


router = APIRouter(tags=["task-rewards"])


@router.post(
    "/projects/{project_id}/task-reward-budgets",
    response_model=TaskRewardBudgetCreateResponse,
    status_code=201,
)
def create_project_task_reward_budget(
    project_id: str,
    payload: TaskRewardBudgetCreateRequest,
    response: Response,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> TaskRewardBudgetCreateResponse:
    get_project_or_404(db, project_id)
    try:
        budget, created = create_task_reward_budget(
            root,
            project_workspace(project_id),
            project_id,
            payload,
        )
    except (
        TaskRewardRoutingNotFoundError,
        TaskRewardProjectNotFoundError,
    ) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (
        TaskRewardRoutingNotFinalizedError,
        TaskRewardProjectMismatchError,
        TaskRewardBudgetConflictError,
        TaskRewardBudgetAlreadyFinalizedError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (TaskRewardBudgetStorageError, TaskRewardBudgetConservationError) as exc:
        raise HTTPException(
            status_code=500,
            detail="Task reward budget failed its storage invariant",
        ) from exc
    except (InvalidTaskRewardIdentifierError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not created:
        response.status_code = 200
    return TaskRewardBudgetCreateResponse(
        status=(
            TaskRewardBudgetProcessingStatus.CREATED
            if created
            else TaskRewardBudgetProcessingStatus.UNCHANGED
        ),
        budget=budget,
        message=(
            "Draft client-funded task reward budget created; no reward was distributed."
            if created
            else "The existing task reward budget was reused; no reward was distributed."
        ),
    )


@router.post(
    "/projects/{project_id}/task-reward-budgets/{task_reward_budget_id}/finalize",
    response_model=TaskRewardBudgetFinalizationResponse,
)
def finalize_project_task_reward_budget(
    project_id: str,
    task_reward_budget_id: str,
    payload: TaskRewardBudgetFinalizeRequest | None = Body(default=None),
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> TaskRewardBudgetFinalizationResponse:
    del payload
    get_project_or_404(db, project_id)
    try:
        budget, finalized = finalize_task_reward_budget(
            root, project_id, task_reward_budget_id
        )
    except TaskRewardBudgetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidTaskRewardIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TaskRewardBudgetAlreadyFinalizedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TaskRewardBudgetStorageError as exc:
        raise HTTPException(
            status_code=500, detail="Stored task reward budget is malformed"
        ) from exc
    return TaskRewardBudgetFinalizationResponse(
        status=(
            TaskRewardBudgetProcessingStatus.FINALIZED
            if finalized
            else TaskRewardBudgetProcessingStatus.ALREADY_FINALIZED
        ),
        budget=budget,
        message=(
            "Task reward budget finalized and made immutable; no reward was distributed."
            if finalized
            else "Task reward budget was already finalized; no reward was distributed."
        ),
    )


@router.get(
    "/projects/{project_id}/task-reward-budgets/{task_reward_budget_id}",
    response_model=TaskRewardBudget,
)
def get_project_task_reward_budget(
    project_id: str,
    task_reward_budget_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> TaskRewardBudget:
    get_project_or_404(db, project_id)
    try:
        budget = load_task_reward_budget(root, project_id, task_reward_budget_id)
    except InvalidTaskRewardIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TaskRewardBudgetStorageError as exc:
        raise HTTPException(
            status_code=500, detail="Stored task reward budget is malformed"
        ) from exc
    if budget is None:
        raise HTTPException(status_code=404, detail="Task reward budget not found")
    return budget


@router.get(
    "/projects/{project_id}/routing/{routing_id}/task-reward-budget",
    response_model=TaskRewardBudget,
)
def get_project_task_reward_budget_by_routing(
    project_id: str,
    routing_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> TaskRewardBudget:
    get_project_or_404(db, project_id)
    try:
        budget = load_task_reward_budget_by_routing(root, routing_id)
    except InvalidTaskRewardIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TaskRewardBudgetStorageError as exc:
        raise HTTPException(
            status_code=500, detail="Stored task reward budget is malformed"
        ) from exc
    if budget is None or budget.project_id != project_id:
        raise HTTPException(status_code=404, detail="Task reward budget not found")
    return budget


@router.get(
    "/projects/{project_id}/task-reward-budgets",
    response_model=list[TaskRewardBudget],
)
def list_project_task_reward_budget_records(
    project_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> list[TaskRewardBudget]:
    get_project_or_404(db, project_id)
    try:
        return list_project_task_reward_budgets(root, project_id)
    except InvalidTaskRewardIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TaskRewardBudgetStorageError as exc:
        raise HTTPException(
            status_code=500, detail="Stored task reward budget is malformed"
        ) from exc
