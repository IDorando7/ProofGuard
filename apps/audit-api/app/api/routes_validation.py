from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.validation import ValidationDecision
from app.services.project_service import get_project_or_404, project_workspace
from app.services.validation_service import list_validation_decisions, load_validation_decision
from app.services.validator_pipeline import FindingNotFoundError, validate_all_findings, validate_finding


router = APIRouter(prefix="/projects", tags=["validation"])


@router.post(
    "/{project_id}/findings/{finding_id}/validate",
    response_model=ValidationDecision,
)
def validate_project_finding(
    project_id: str,
    finding_id: str,
    db: Session = Depends(get_db),
) -> ValidationDecision:
    get_project_or_404(db, project_id)
    try:
        return validate_finding(project_id, finding_id, project_workspace(project_id))
    except FindingNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Finding not found") from exc


@router.get(
    "/{project_id}/findings/{finding_id}/validation",
    response_model=ValidationDecision,
)
def get_finding_validation(
    project_id: str,
    finding_id: str,
    db: Session = Depends(get_db),
) -> ValidationDecision:
    get_project_or_404(db, project_id)
    decision = load_validation_decision(project_workspace(project_id), finding_id)
    if decision is None:
        raise HTTPException(status_code=404, detail="Validation decision not found")
    return decision


@router.get(
    "/{project_id}/validations",
    response_model=list[ValidationDecision],
)
def list_project_validations(
    project_id: str,
    db: Session = Depends(get_db),
) -> list[ValidationDecision]:
    get_project_or_404(db, project_id)
    return list_validation_decisions(project_workspace(project_id))


@router.post(
    "/{project_id}/validate-all",
    response_model=list[ValidationDecision],
)
def validate_project_findings(
    project_id: str,
    db: Session = Depends(get_db),
) -> list[ValidationDecision]:
    get_project_or_404(db, project_id)
    return validate_all_findings(project_id, project_workspace(project_id))
