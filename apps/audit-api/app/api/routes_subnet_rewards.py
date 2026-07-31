from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.paths import protocol_data_root
from app.schemas.subnet_reward import (
    SubnetRewardCalculationResponse,
    SubnetRewardCycle,
    SubnetRewardCycleCreateRequest,
    SubnetRewardCycleCreateResponse,
    SubnetRewardCycleStatus,
    SubnetRewardEventListResponse,
    SubnetRewardFinalizationResponse,
    SubnetRewardProcessingStatus,
)
from app.services.node_registry_service import (
    InvalidNodeIdentifierError,
    load_node,
)
from app.services.project_service import get_project_or_404, project_workspace
from app.services.submission_service import (
    InvalidSubmissionIdentifierError,
    load_submission,
)
from app.services.subnet_registry_service import (
    InvalidSubnetIdentifierError,
    load_subnet,
)
from app.services.subnet_reward_allocation_service import (
    InvalidSubnetRewardIdentifierError,
    SubnetRewardAlreadyFinalizedError,
    SubnetRewardAlreadyPaidError,
    SubnetRewardConservationError,
    SubnetRewardCycleConflictError,
    SubnetRewardCycleNotFoundError,
    SubnetRewardDuplicateEventError,
    SubnetRewardInvalidCategoryWeightsError,
    SubnetRewardProjectNotFoundError,
    SubnetRewardRoutingNotFinalizedError,
    SubnetRewardRoutingNotFoundError,
    SubnetRewardSourceChangedError,
    SubnetRewardSourceMismatchError,
    SubnetRewardStorageError,
    calculate_subnet_reward_cycle,
    create_subnet_reward_cycle,
    finalize_subnet_reward_cycle,
    find_subnet_reward_cycle_by_routing,
    list_project_subnet_reward_cycles,
    list_subnet_reward_events,
    load_subnet_reward_cycle,
)


router = APIRouter(tags=["subnet-rewards"])


@router.post(
    "/projects/{project_id}/subnet-reward-cycles",
    response_model=SubnetRewardCycleCreateResponse,
    status_code=201,
)
def create_reward_cycle(
    project_id: str,
    payload: SubnetRewardCycleCreateRequest,
    response: Response,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> SubnetRewardCycleCreateResponse:
    get_project_or_404(db, project_id)
    try:
        previous = find_subnet_reward_cycle_by_routing(root, payload.routing_id)
        cycle = create_subnet_reward_cycle(
            root,
            project_workspace(project_id),
            project_id,
            payload,
        )
    except (
        SubnetRewardRoutingNotFoundError,
        SubnetRewardProjectNotFoundError,
    ) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (
        SubnetRewardRoutingNotFinalizedError,
        SubnetRewardCycleConflictError,
        SubnetRewardSourceMismatchError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (
        SubnetRewardInvalidCategoryWeightsError,
        InvalidSubnetRewardIdentifierError,
    ) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SubnetRewardStorageError as exc:
        raise HTTPException(
            status_code=500,
            detail="Stored subnet reward data is malformed",
        ) from exc
    if previous is not None and previous.reward_cycle_id == cycle.reward_cycle_id:
        response.status_code = 200
        status = SubnetRewardProcessingStatus.UNCHANGED
        message = "The existing reward cycle was reused."
    else:
        status = SubnetRewardProcessingStatus.CREATED
        message = "Draft subnet reward cycle created."
    return SubnetRewardCycleCreateResponse(
        status=status,
        cycle=cycle,
        message=message,
    )


@router.post(
    "/projects/{project_id}/subnet-reward-cycles/{reward_cycle_id}/calculate",
    response_model=SubnetRewardCalculationResponse,
)
def calculate_reward_cycle(
    project_id: str,
    reward_cycle_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> SubnetRewardCalculationResponse:
    get_project_or_404(db, project_id)
    try:
        return calculate_subnet_reward_cycle(
            root,
            project_workspace(project_id),
            project_id,
            reward_cycle_id,
        )
    except SubnetRewardCycleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (
        SubnetRewardAlreadyFinalizedError,
        SubnetRewardRoutingNotFinalizedError,
        SubnetRewardSourceChangedError,
        SubnetRewardSourceMismatchError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except InvalidSubnetRewardIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (SubnetRewardStorageError, SubnetRewardConservationError) as exc:
        raise HTTPException(
            status_code=500,
            detail="Subnet reward calculation failed its stored-data invariant",
        ) from exc


@router.post(
    "/projects/{project_id}/subnet-reward-cycles/{reward_cycle_id}/finalize",
    response_model=SubnetRewardFinalizationResponse,
)
def finalize_reward_cycle(
    project_id: str,
    reward_cycle_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> SubnetRewardFinalizationResponse:
    get_project_or_404(db, project_id)
    try:
        return finalize_subnet_reward_cycle(
            root,
            project_workspace(project_id),
            project_id,
            reward_cycle_id,
        )
    except SubnetRewardCycleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (
        SubnetRewardCycleConflictError,
        SubnetRewardSourceChangedError,
        SubnetRewardSourceMismatchError,
        SubnetRewardAlreadyPaidError,
        SubnetRewardDuplicateEventError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except InvalidSubnetRewardIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (SubnetRewardStorageError, SubnetRewardConservationError) as exc:
        raise HTTPException(
            status_code=500,
            detail="Subnet reward finalization failed its stored-data invariant",
        ) from exc


@router.get(
    "/projects/{project_id}/subnet-reward-cycles/{reward_cycle_id}",
    response_model=SubnetRewardCycle,
)
def get_reward_cycle(
    project_id: str,
    reward_cycle_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> SubnetRewardCycle:
    get_project_or_404(db, project_id)
    try:
        cycle = load_subnet_reward_cycle(root, project_id, reward_cycle_id)
    except InvalidSubnetRewardIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if cycle is None:
        raise HTTPException(status_code=404, detail="Subnet reward cycle not found")
    return cycle


@router.get(
    "/projects/{project_id}/subnet-reward-cycles",
    response_model=list[SubnetRewardCycle],
)
def list_reward_cycles(
    project_id: str,
    status: SubnetRewardCycleStatus | None = None,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> list[SubnetRewardCycle]:
    get_project_or_404(db, project_id)
    try:
        return list_project_subnet_reward_cycles(root, project_id, status=status)
    except InvalidSubnetRewardIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/projects/{project_id}/routing/{routing_id}/subnet-reward-cycle",
    response_model=SubnetRewardCycle,
)
def get_reward_cycle_by_routing(
    project_id: str,
    routing_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> SubnetRewardCycle:
    get_project_or_404(db, project_id)
    try:
        cycle = find_subnet_reward_cycle_by_routing(root, routing_id)
    except InvalidSubnetRewardIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if cycle is None or cycle.project_id != project_id:
        raise HTTPException(status_code=404, detail="Subnet reward cycle not found")
    return cycle


@router.get(
    "/nodes/{node_id}/subnet-rewards",
    response_model=SubnetRewardEventListResponse,
)
def list_node_rewards(
    node_id: str,
    project_id: str | None = None,
    category: str | None = None,
    subnet_id: str | None = None,
    root: Path = Depends(protocol_data_root),
) -> SubnetRewardEventListResponse:
    try:
        if load_node(root, node_id) is None:
            raise HTTPException(status_code=404, detail="Node not found")
        events = list_subnet_reward_events(
            root,
            node_id=node_id,
            project_id=project_id,
            category=category,
            subnet_id=subnet_id,
        )
    except (
        InvalidNodeIdentifierError,
        InvalidSubnetRewardIdentifierError,
        ValueError,
    ) as exc:
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SubnetRewardEventListResponse(total=len(events), events=events)


@router.get(
    "/subnets/{subnet_id}/reward-events",
    response_model=SubnetRewardEventListResponse,
)
def list_subnet_rewards(
    subnet_id: str,
    project_id: str | None = None,
    node_id: str | None = None,
    root: Path = Depends(protocol_data_root),
) -> SubnetRewardEventListResponse:
    try:
        if load_subnet(root, subnet_id) is None:
            raise HTTPException(status_code=404, detail="Subnet not found")
        events = list_subnet_reward_events(
            root,
            subnet_id=subnet_id,
            project_id=project_id,
            node_id=node_id,
        )
    except (
        InvalidSubnetIdentifierError,
        InvalidSubnetRewardIdentifierError,
    ) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SubnetRewardEventListResponse(total=len(events), events=events)


@router.get(
    "/submissions/{submission_id}/subnet-rewards",
    response_model=SubnetRewardEventListResponse,
)
def list_submission_rewards(
    submission_id: str,
    root: Path = Depends(protocol_data_root),
) -> SubnetRewardEventListResponse:
    try:
        if load_submission(root, submission_id) is None:
            raise HTTPException(status_code=404, detail="Submission not found")
        events = list_subnet_reward_events(
            root,
            submission_id=submission_id,
        )
    except (
        InvalidSubmissionIdentifierError,
        InvalidSubnetRewardIdentifierError,
    ) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SubnetRewardEventListResponse(total=len(events), events=events)
