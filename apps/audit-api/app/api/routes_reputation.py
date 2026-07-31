from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.paths import protocol_data_root
from app.schemas.reputation import (
    NodeReputationResponse,
    ReputationEvent,
    ReputationEventApplicationStatus,
    ReputationEventType,
    ReputationProcessRequest,
    ReputationProcessResponse,
)
from app.services.node_registry_service import InvalidNodeIdentifierError, NodeStorageError
from app.services.project_service import get_project_or_404, project_workspace
from app.services.reputation_service import (
    InvalidReputationIdentifierError,
    ReputationContributionNotFoundError,
    ReputationFindingNotFoundError,
    ReputationInputMismatchError,
    ReputationNodeNotFoundError,
    ReputationProcessingError,
    ReputationSourceChangedError,
    ReputationStorageError,
    ReputationSubmissionNotFoundError,
    get_node_reputation,
    get_node_reputation_history,
    list_reputation_events,
    load_reputation_event_by_id,
    process_submission_reputation,
)
from app.services.submission_service import (
    InvalidSubmissionIdentifierError,
    SubmissionStorageError,
    load_submission,
)


router = APIRouter(tags=["reputation"])


@router.post(
    "/submissions/{submission_id}/reputation/process",
    response_model=ReputationProcessResponse,
)
def process_reputation(
    submission_id: str,
    payload: ReputationProcessRequest | None = Body(default=None),
    root: Path = Depends(protocol_data_root),
) -> ReputationProcessResponse:
    del payload
    try:
        submission = load_submission(root, submission_id)
    except InvalidSubmissionIdentifierError as exc:
        raise HTTPException(status_code=400, detail="Invalid submission identifier") from exc
    except SubmissionStorageError as exc:
        raise HTTPException(status_code=500, detail="Stored submission record is malformed") from exc
    if submission is None:
        raise HTTPException(status_code=404, detail="Submission not found")

    try:
        return process_submission_reputation(
            root,
            project_workspace(submission.project_id),
            submission_id,
        )
    except ReputationSubmissionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ReputationNodeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ReputationFindingNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ReputationContributionNotFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ReputationInputMismatchError, ReputationSourceChangedError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except InvalidReputationIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (ReputationProcessingError, ReputationStorageError, NodeStorageError) as exc:
        raise HTTPException(status_code=500, detail="Reputation processing failed") from exc


@router.get("/nodes/{node_id}/reputation", response_model=NodeReputationResponse)
def read_node_reputation(
    node_id: str,
    root: Path = Depends(protocol_data_root),
) -> NodeReputationResponse:
    try:
        return get_node_reputation(root, node_id)
    except ReputationNodeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (InvalidNodeIdentifierError, InvalidReputationIdentifierError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (ReputationStorageError, NodeStorageError) as exc:
        raise HTTPException(status_code=500, detail="Unable to read node reputation") from exc


@router.get("/nodes/{node_id}/reputation/history", response_model=list[ReputationEvent])
def read_node_reputation_history(
    node_id: str,
    category: str | None = None,
    event_type: ReputationEventType | None = None,
    application_status: ReputationEventApplicationStatus | None = None,
    root: Path = Depends(protocol_data_root),
) -> list[ReputationEvent]:
    try:
        return get_node_reputation_history(
            root,
            node_id,
            category=category,
            event_type=event_type,
            application_status=application_status,
        )
    except ReputationNodeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ReputationStorageError, NodeStorageError) as exc:
        raise HTTPException(status_code=500, detail="Unable to read reputation history") from exc
    except (InvalidNodeIdentifierError, InvalidReputationIdentifierError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/reputation/events/{event_id}", response_model=ReputationEvent)
def read_reputation_event(
    event_id: str,
    root: Path = Depends(protocol_data_root),
) -> ReputationEvent:
    try:
        event = load_reputation_event_by_id(root, event_id)
    except InvalidReputationIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ReputationStorageError as exc:
        raise HTTPException(status_code=500, detail="Unable to read reputation event") from exc
    if event is None:
        raise HTTPException(status_code=404, detail="Reputation event not found")
    return event


@router.get(
    "/projects/{project_id}/reputation/events",
    response_model=list[ReputationEvent],
)
def read_project_reputation_events(
    project_id: str,
    node_id: str | None = None,
    category: str | None = None,
    event_type: ReputationEventType | None = None,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> list[ReputationEvent]:
    get_project_or_404(db, project_id)
    try:
        return list_reputation_events(
            root,
            node_id=node_id,
            project_id=project_id,
            category=category,
            event_type=event_type,
        )
    except ReputationStorageError as exc:
        raise HTTPException(status_code=500, detail="Unable to read project reputation events") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
