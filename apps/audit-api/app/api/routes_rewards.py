from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, status as http_status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.paths import protocol_data_root
from app.schemas.reward import (
    NodeRewardSummary,
    PenaltyEvent,
    PenaltyProcessRequest,
    PenaltyProcessResponse,
    RewardCycle,
    RewardCycleActionRequest,
    RewardCycleCreate,
    RewardCycleRequest,
    RewardCycleStatus,
    RewardEvent,
    RewardEventStatus,
)
from app.services.node_registry_service import InvalidNodeIdentifierError, load_node
from app.services.penalty_service import (
    InvalidPenaltyIdentifierError,
    PenaltyEventNotFoundError,
    PenaltyInputMismatchError,
    PenaltyNotApplicableError,
    PenaltySourceChangedError,
    PenaltyStorageError,
    list_penalty_events,
    load_penalty_event_by_submission,
    process_submission_penalty,
)
from app.services.project_service import get_project_or_404, project_workspace
from app.services.reward_service import (
    InvalidRewardIdentifierError,
    RewardCycleAlreadyFinalizedError,
    RewardCycleNotCalculatedError,
    RewardCycleNotFoundError,
    RewardCycleSourceChangedError,
    RewardEventConflictError,
    RewardInputMismatchError,
    RewardStorageError,
    RewardSubmissionNotFoundError,
    calculate_reward_cycle,
    create_reward_cycle,
    finalize_reward_cycle,
    get_node_reward_summary,
    list_reward_cycles,
    load_reward_cycle,
    load_reward_event_by_submission,
)
from app.services.submission_service import (
    InvalidSubmissionIdentifierError,
    SubmissionStorageError,
    load_submission,
)


router = APIRouter(tags=["rewards"])


@router.post(
    "/projects/{project_id}/reward-cycles",
    response_model=RewardCycle,
    status_code=http_status.HTTP_201_CREATED,
)
def create_project_reward_cycle(
    project_id: str,
    payload: RewardCycleRequest,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> RewardCycle:
    get_project_or_404(db, project_id)
    try:
        return create_reward_cycle(
            root,
            project_workspace(project_id),
            RewardCycleCreate(project_id=project_id, **payload.model_dump()),
        )
    except RewardSubmissionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidRewardIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RewardInputMismatchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RewardStorageError as exc:
        raise HTTPException(status_code=500, detail="Unable to create reward cycle") from exc


@router.post("/reward-cycles/{cycle_id}/calculate", response_model=RewardCycle)
def calculate_cycle(
    cycle_id: str,
    payload: RewardCycleActionRequest | None = Body(default=None),
    root: Path = Depends(protocol_data_root),
) -> RewardCycle:
    del payload
    cycle = _read_cycle_or_http(root, cycle_id)
    try:
        return calculate_reward_cycle(root, project_workspace(cycle.project_id), cycle_id)
    except RewardSubmissionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (RewardCycleAlreadyFinalizedError, RewardInputMismatchError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except InvalidRewardIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RewardStorageError as exc:
        raise HTTPException(status_code=500, detail="Unable to calculate reward cycle") from exc


@router.post("/reward-cycles/{cycle_id}/finalize", response_model=RewardCycle)
def finalize_cycle(
    cycle_id: str,
    payload: RewardCycleActionRequest | None = Body(default=None),
    root: Path = Depends(protocol_data_root),
) -> RewardCycle:
    del payload
    cycle = _read_cycle_or_http(root, cycle_id)
    try:
        return finalize_reward_cycle(root, project_workspace(cycle.project_id), cycle_id)
    except RewardSubmissionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (
        RewardCycleNotCalculatedError,
        RewardCycleSourceChangedError,
        RewardEventConflictError,
        RewardInputMismatchError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except InvalidRewardIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RewardStorageError as exc:
        raise HTTPException(status_code=500, detail="Unable to finalize reward cycle") from exc


@router.get("/reward-cycles/{cycle_id}", response_model=RewardCycle)
def read_reward_cycle(
    cycle_id: str,
    root: Path = Depends(protocol_data_root),
) -> RewardCycle:
    return _read_cycle_or_http(root, cycle_id)


@router.get("/projects/{project_id}/reward-cycles", response_model=list[RewardCycle])
def read_project_reward_cycles(
    project_id: str,
    status: RewardCycleStatus | None = None,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> list[RewardCycle]:
    get_project_or_404(db, project_id)
    try:
        return list_reward_cycles(root, project_id=project_id, status=status)
    except RewardStorageError as exc:
        raise HTTPException(status_code=500, detail="Unable to list reward cycles") from exc


@router.get("/submissions/{submission_id}/reward", response_model=RewardEvent)
def read_submission_reward(
    submission_id: str,
    root: Path = Depends(protocol_data_root),
) -> RewardEvent:
    try:
        event = load_reward_event_by_submission(root, submission_id)
    except InvalidRewardIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RewardStorageError as exc:
        raise HTTPException(status_code=500, detail="Unable to read RewardEvent") from exc
    if event is None or event.event_status != RewardEventStatus.FINALIZED:
        raise HTTPException(status_code=404, detail="RewardEvent not found")
    return event


@router.get("/nodes/{node_id}/rewards", response_model=NodeRewardSummary)
def read_node_rewards(
    node_id: str,
    root: Path = Depends(protocol_data_root),
) -> NodeRewardSummary:
    try:
        node = load_node(root, node_id)
    except InvalidNodeIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found")
    try:
        return get_node_reward_summary(root, node_id)
    except InvalidRewardIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RewardStorageError as exc:
        raise HTTPException(status_code=500, detail="Unable to read node rewards") from exc


@router.post(
    "/submissions/{submission_id}/penalty/process",
    response_model=PenaltyProcessResponse,
)
def process_unsafe_penalty(
    submission_id: str,
    payload: PenaltyProcessRequest | None = Body(default=None),
    root: Path = Depends(protocol_data_root),
) -> PenaltyProcessResponse:
    del payload
    submission = _read_submission_or_http(root, submission_id)
    try:
        return process_submission_penalty(
            root,
            project_workspace(submission.project_id),
            submission_id,
        )
    except PenaltyEventNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (PenaltyNotApplicableError, PenaltyInputMismatchError, PenaltySourceChangedError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except InvalidPenaltyIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PenaltyStorageError as exc:
        raise HTTPException(status_code=500, detail="Unable to process PenaltyEvent") from exc


@router.get("/submissions/{submission_id}/penalty", response_model=PenaltyEvent)
def read_submission_penalty(
    submission_id: str,
    root: Path = Depends(protocol_data_root),
) -> PenaltyEvent:
    try:
        event = load_penalty_event_by_submission(root, submission_id)
    except InvalidPenaltyIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PenaltyStorageError as exc:
        raise HTTPException(status_code=500, detail="Unable to read PenaltyEvent") from exc
    if event is None:
        raise HTTPException(status_code=404, detail="PenaltyEvent not found")
    return event


@router.get("/nodes/{node_id}/penalties", response_model=list[PenaltyEvent])
def read_node_penalties(
    node_id: str,
    root: Path = Depends(protocol_data_root),
) -> list[PenaltyEvent]:
    try:
        node = load_node(root, node_id)
    except InvalidNodeIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found")
    try:
        return list_penalty_events(root, node_id=node_id)
    except PenaltyStorageError as exc:
        raise HTTPException(status_code=500, detail="Unable to list node penalties") from exc


def _read_cycle_or_http(root: Path, cycle_id: str) -> RewardCycle:
    try:
        cycle = load_reward_cycle(root, cycle_id)
    except InvalidRewardIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RewardStorageError as exc:
        raise HTTPException(status_code=500, detail="Unable to read reward cycle") from exc
    if cycle is None:
        raise HTTPException(status_code=404, detail="Reward cycle not found")
    return cycle


def _read_submission_or_http(root: Path, submission_id: str):
    try:
        submission = load_submission(root, submission_id)
    except InvalidSubmissionIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SubmissionStorageError as exc:
        raise HTTPException(status_code=500, detail="Stored submission record is malformed") from exc
    if submission is None:
        raise HTTPException(status_code=404, detail="Submission not found")
    return submission
