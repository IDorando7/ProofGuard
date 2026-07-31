from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.paths import protocol_data_root
from app.schemas.contribution import (
    ContributionCalculationRequest,
    ContributionEligibilityStatus,
    ContributionScoreRecord,
)
from app.services.contribution_scoring_service import (
    ContributionCalculationError,
    ContributionFindingNotFoundError,
    ContributionInputMismatchError,
    ContributionStorageError,
    ContributionSubmissionNotFoundError,
    InvalidContributionIdentifierError,
    calculate_contribution_for_submission,
    list_contribution_scores,
    load_contribution_score,
)
from app.services.node_registry_service import InvalidNodeIdentifierError, load_node
from app.services.project_service import get_project_or_404, project_workspace
from app.services.submission_service import (
    InvalidSubmissionIdentifierError,
    SubmissionStorageError,
    load_submission,
)


router = APIRouter(tags=["contributions"])


@router.post(
    "/submissions/{submission_id}/contribution/calculate",
    response_model=ContributionScoreRecord,
)
def calculate_submission_contribution(
    submission_id: str,
    payload: ContributionCalculationRequest | None = Body(default=None),
    root: Path = Depends(protocol_data_root),
) -> ContributionScoreRecord:
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
        return calculate_contribution_for_submission(
            root,
            project_workspace(submission.project_id),
            submission_id,
        )
    except ContributionSubmissionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ContributionFindingNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ContributionInputMismatchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except InvalidContributionIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (ContributionCalculationError, ContributionStorageError) as exc:
        raise HTTPException(status_code=500, detail="Contribution calculation failed") from exc


@router.get(
    "/submissions/{submission_id}/contribution",
    response_model=ContributionScoreRecord,
)
def get_submission_contribution(
    submission_id: str,
    root: Path = Depends(protocol_data_root),
) -> ContributionScoreRecord:
    try:
        record = load_contribution_score(root, submission_id)
    except InvalidContributionIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ContributionStorageError as exc:
        raise HTTPException(status_code=500, detail="Stored contribution record is malformed") from exc
    if record is None:
        raise HTTPException(status_code=404, detail="Contribution score not found")
    return record


@router.get(
    "/projects/{project_id}/contributions",
    response_model=list[ContributionScoreRecord],
)
def get_project_contributions(
    project_id: str,
    node_id: str | None = None,
    eligibility_status: ContributionEligibilityStatus | None = None,
    eligible_for_reward: bool | None = None,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> list[ContributionScoreRecord]:
    get_project_or_404(db, project_id)
    return list_contribution_scores(
        root,
        project_id=project_id,
        node_id=node_id,
        eligibility_status=eligibility_status,
        eligible_for_reward=eligible_for_reward,
    )


@router.get(
    "/nodes/{node_id}/contributions",
    response_model=list[ContributionScoreRecord],
)
def get_node_contributions(
    node_id: str,
    project_id: str | None = None,
    eligibility_status: ContributionEligibilityStatus | None = None,
    eligible_for_reward: bool | None = None,
    root: Path = Depends(protocol_data_root),
) -> list[ContributionScoreRecord]:
    try:
        node = load_node(root, node_id)
    except InvalidNodeIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found")
    return list_contribution_scores(
        root,
        project_id=project_id,
        node_id=node_id,
        eligibility_status=eligibility_status,
        eligible_for_reward=eligible_for_reward,
    )
