from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status as http_status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.paths import protocol_data_root
from app.schemas.submission import (
    SubmissionCreate,
    SubmissionRecord,
    SubmissionRequest,
    SubmissionRewardStatus,
    SubmissionStatus,
)
from app.services.node_registry_service import (
    InvalidNodeIdentifierError,
    NodeInactiveError,
    NodeNotFoundError,
    UnsupportedNodeCategoryError,
    load_node,
)
from app.services.project_service import get_project_or_404, project_workspace
from app.services.submission_service import (
    DuplicateSubmissionError,
    FindingNotFoundForSubmissionError,
    InvalidSubmissionIdentifierError,
    NodeCannotSubmitError,
    SubmissionProjectMismatchError,
    SubmissionRoutingMismatchError,
    create_submission,
    list_submissions,
    load_submission,
)


router = APIRouter(tags=["submissions"])


@router.post(
    "/projects/{project_id}/submissions",
    response_model=SubmissionRecord,
    status_code=http_status.HTTP_201_CREATED,
)
def submit_project_finding(
    project_id: str,
    payload: SubmissionRequest,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> SubmissionRecord:
    get_project_or_404(db, project_id)
    request = SubmissionCreate(project_id=project_id, **payload.model_dump())
    try:
        return create_submission(root, project_workspace(project_id), request)
    except FindingNotFoundForSubmissionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except NodeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (NodeInactiveError, NodeCannotSubmitError) as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except UnsupportedNodeCategoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SubmissionProjectMismatchError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SubmissionRoutingMismatchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DuplicateSubmissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except InvalidNodeIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/projects/{project_id}/submissions", response_model=list[SubmissionRecord])
def get_project_submissions(
    project_id: str,
    node_id: str | None = None,
    status: SubmissionStatus | None = None,
    category: str | None = None,
    reward_status: SubmissionRewardStatus | None = None,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> list[SubmissionRecord]:
    get_project_or_404(db, project_id)
    try:
        return list_submissions(
            root,
            project_id=project_id,
            node_id=node_id,
            status=status,
            category=category,
            reward_status=reward_status,
        )
    except UnsupportedNodeCategoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/submissions/{submission_id}", response_model=SubmissionRecord)
def get_submission(
    submission_id: str,
    root: Path = Depends(protocol_data_root),
) -> SubmissionRecord:
    try:
        submission = load_submission(root, submission_id)
    except InvalidSubmissionIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if submission is None:
        raise HTTPException(status_code=404, detail="Submission not found")
    return submission


@router.get("/nodes/{node_id}/submissions", response_model=list[SubmissionRecord])
def get_node_submissions(
    node_id: str,
    project_id: str | None = None,
    status: SubmissionStatus | None = None,
    category: str | None = None,
    reward_status: SubmissionRewardStatus | None = None,
    root: Path = Depends(protocol_data_root),
) -> list[SubmissionRecord]:
    try:
        node = load_node(root, node_id)
    except InvalidNodeIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found")
    try:
        return list_submissions(
            root,
            project_id=project_id,
            node_id=node_id,
            status=status,
            category=category,
            reward_status=reward_status,
        )
    except UnsupportedNodeCategoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
