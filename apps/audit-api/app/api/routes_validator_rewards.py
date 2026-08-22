from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.paths import protocol_data_root
from app.schemas.validator_reward import (
    ValidatorRewardAllocationListResponse,
    ValidatorRewardCycle,
    ValidatorRewardCycleActionRequest,
    ValidatorRewardCycleCreateRequest,
    ValidatorRewardCycleListResponse,
    ValidatorRewardCycleResponse,
    ValidatorRewardCycleVerification,
    ValidatorRewardEventListResponse,
    ValidatorRewardProcessingStatus,
)
from app.services.project_service import get_project_or_404
from app.services.validator_reward_cycle_service import (
    ValidatorRewardCycleConflictError,
    ValidatorRewardCycleNotFoundError,
    ValidatorRewardCycleSourceChangedError,
    ValidatorRewardCycleStateError,
    ValidatorRewardCycleStorageError,
    calculate_validator_reward_cycle,
    create_validator_reward_cycle,
    finalize_validator_reward_cycle,
    list_validator_reward_cycles,
    list_validator_reward_events,
    load_validator_reward_cycle,
    verify_validator_reward_cycle,
)


router = APIRouter(tags=["validator-rewards"])


def _raise(exc: Exception) -> None:
    if isinstance(exc, ValidatorRewardCycleNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, (ValidatorRewardCycleConflictError, ValidatorRewardCycleSourceChangedError, ValidatorRewardCycleStateError)):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, ValidatorRewardCycleStorageError):
        raise HTTPException(status_code=500, detail="Validator reward storage invariant failed") from exc
    raise exc


@router.post(
    "/projects/{project_id}/routing/{routing_id}/validator-reward-cycles",
    response_model=ValidatorRewardCycleResponse,
    status_code=201,
)
def create_cycle(project_id: str, routing_id: str, payload: ValidatorRewardCycleCreateRequest, response: Response, db: Session = Depends(get_db), root: Path = Depends(protocol_data_root)) -> ValidatorRewardCycleResponse:
    get_project_or_404(db, project_id)
    try:
        cycle, created = create_validator_reward_cycle(root, project_id=project_id, routing_id=routing_id, request=payload)
    except Exception as exc:
        _raise(exc)
    if not created:
        response.status_code = 200
    return ValidatorRewardCycleResponse(
        processing_status=ValidatorRewardProcessingStatus.CREATED if created else ValidatorRewardProcessingStatus.UNCHANGED,
        cycle=cycle,
        message="Draft validator reward cycle created; no RewardEvent emitted." if created else "Existing validator reward cycle reused.",
    )


@router.post(
    "/projects/{project_id}/routing/{routing_id}/validator-reward-cycles/{reward_cycle_id}/calculate",
    response_model=ValidatorRewardCycleResponse,
)
def calculate_cycle(project_id: str, routing_id: str, reward_cycle_id: str, payload: ValidatorRewardCycleActionRequest | None = Body(default=None), db: Session = Depends(get_db), root: Path = Depends(protocol_data_root)) -> ValidatorRewardCycleResponse:
    del payload
    get_project_or_404(db, project_id)
    try:
        cycle, status = calculate_validator_reward_cycle(root, project_id=project_id, routing_id=routing_id, reward_cycle_id=reward_cycle_id)
    except Exception as exc:
        _raise(exc)
    return ValidatorRewardCycleResponse(processing_status=status, cycle=cycle, message="Validator reward snapshot calculated; no RewardEvent emitted." if status != ValidatorRewardProcessingStatus.UNCHANGED else "Validator reward sources unchanged.")


@router.post(
    "/projects/{project_id}/routing/{routing_id}/validator-reward-cycles/{reward_cycle_id}/finalize",
    response_model=ValidatorRewardCycleResponse,
)
def finalize_cycle(project_id: str, routing_id: str, reward_cycle_id: str, payload: ValidatorRewardCycleActionRequest | None = Body(default=None), db: Session = Depends(get_db), root: Path = Depends(protocol_data_root)) -> ValidatorRewardCycleResponse:
    del payload
    get_project_or_404(db, project_id)
    try:
        cycle, status, created, existing = finalize_validator_reward_cycle(root, project_id=project_id, routing_id=routing_id, reward_cycle_id=reward_cycle_id)
    except Exception as exc:
        _raise(exc)
    return ValidatorRewardCycleResponse(processing_status=status, cycle=cycle, reward_events_created=created, reward_events_existing=existing, message="Validator pool finalized exactly once; positive allocations are immutable RewardEvents." if status == ValidatorRewardProcessingStatus.FINALIZED else "Validator reward cycle was already finalized; no duplicate payout occurred.")


@router.get(
    "/projects/{project_id}/routing/{routing_id}/validator-reward-cycles/{reward_cycle_id}",
    response_model=ValidatorRewardCycle,
)
def get_cycle(project_id: str, routing_id: str, reward_cycle_id: str, db: Session = Depends(get_db), root: Path = Depends(protocol_data_root)) -> ValidatorRewardCycle:
    get_project_or_404(db, project_id)
    try:
        cycle = load_validator_reward_cycle(root, project_id, routing_id, reward_cycle_id)
    except Exception as exc:
        _raise(exc)
    if cycle is None:
        raise HTTPException(status_code=404, detail="Validator reward cycle not found")
    return cycle


@router.get(
    "/projects/{project_id}/routing/{routing_id}/validator-reward-cycles",
    response_model=ValidatorRewardCycleListResponse,
)
def list_cycles(project_id: str, routing_id: str, db: Session = Depends(get_db), root: Path = Depends(protocol_data_root)) -> ValidatorRewardCycleListResponse:
    get_project_or_404(db, project_id)
    values = list_validator_reward_cycles(root, project_id, routing_id)
    return ValidatorRewardCycleListResponse(total=len(values), cycles=values)


@router.get(
    "/projects/{project_id}/routing/{routing_id}/validator-reward-cycles/{reward_cycle_id}/allocations",
    response_model=ValidatorRewardAllocationListResponse,
)
def list_allocations(project_id: str, routing_id: str, reward_cycle_id: str, db: Session = Depends(get_db), root: Path = Depends(protocol_data_root)) -> ValidatorRewardAllocationListResponse:
    cycle = get_cycle(project_id, routing_id, reward_cycle_id, db, root)
    return ValidatorRewardAllocationListResponse(total=len(cycle.allocations), allocations=cycle.allocations)


@router.get(
    "/projects/{project_id}/routing/{routing_id}/validator-reward-cycles/{reward_cycle_id}/events",
    response_model=ValidatorRewardEventListResponse,
)
def list_events(project_id: str, routing_id: str, reward_cycle_id: str, db: Session = Depends(get_db), root: Path = Depends(protocol_data_root)) -> ValidatorRewardEventListResponse:
    get_cycle(project_id, routing_id, reward_cycle_id, db, root)
    events = list_validator_reward_events(root, project_id, routing_id, reward_cycle_id)
    return ValidatorRewardEventListResponse(total=len(events), events=events)


@router.get(
    "/projects/{project_id}/routing/{routing_id}/validator-reward-cycles/{reward_cycle_id}/verify",
    response_model=ValidatorRewardCycleVerification,
)
def verify_cycle(project_id: str, routing_id: str, reward_cycle_id: str, db: Session = Depends(get_db), root: Path = Depends(protocol_data_root)) -> ValidatorRewardCycleVerification:
    get_project_or_404(db, project_id)
    try:
        return verify_validator_reward_cycle(root, project_id=project_id, routing_id=routing_id, reward_cycle_id=reward_cycle_id)
    except Exception as exc:
        _raise(exc)
