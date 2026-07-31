from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.report import FinalAuditReport, ReportGenerationResponse
from app.services.project_service import get_project_or_404, project_workspace
from app.services.report_service import (
    load_final_audit_report_json,
    load_final_audit_report_markdown,
    save_final_audit_report,
)


router = APIRouter(prefix="/projects", tags=["reports"])


@router.post(
    "/{project_id}/reports/final",
    response_model=ReportGenerationResponse,
)
def generate_final_report(
    project_id: str,
    db: Session = Depends(get_db),
) -> ReportGenerationResponse:
    get_project_or_404(db, project_id)
    return save_final_audit_report(project_id, project_workspace(project_id))


@router.get(
    "/{project_id}/reports/final",
    response_model=FinalAuditReport,
)
def get_final_report(
    project_id: str,
    db: Session = Depends(get_db),
) -> FinalAuditReport:
    get_project_or_404(db, project_id)
    report = load_final_audit_report_json(project_workspace(project_id))
    if report is None:
        raise HTTPException(status_code=404, detail="Final report not found")
    return report


@router.get(
    "/{project_id}/reports/final/markdown",
    response_class=PlainTextResponse,
)
def get_final_report_markdown(
    project_id: str,
    db: Session = Depends(get_db),
) -> PlainTextResponse:
    get_project_or_404(db, project_id)
    markdown = load_final_audit_report_markdown(project_workspace(project_id))
    if markdown is None:
        raise HTTPException(status_code=404, detail="Final report markdown not found")
    return PlainTextResponse(markdown)
