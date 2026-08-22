from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.paths import protocol_data_root
from app.schemas.report_quality import (
    ReportQualityAssessment,
    ReportQualityAssessmentListResponse,
    ReportQualityAssessmentRequest,
    ReportQualityRebuildRequest,
    ReportQualityRebuildResult,
)
from app.services.project_service import get_project_or_404, project_workspace
from app.services.report_quality_assessment_service import (
    ReportQualityConflictError,
    ReportQualityEligibilityError,
    ReportQualityLinkageError,
    ReportQualityNotFoundError,
    ReportQualityStorageError,
    assess_report_quality,
    list_cluster_report_quality_assessments,
    load_active_report_quality_assessment,
    load_report_quality_assessment_by_id,
    rebuild_task_report_quality_assessments,
)


router = APIRouter(tags=["report-quality"])


@router.post(
    "/projects/{project_id}/routing/{routing_id}/finding-clusters/{finding_cluster_id}/submissions/{submission_id}/quality-assessment",
    response_model=ReportQualityAssessment,
)
def create_report_quality_assessment(
    project_id: str,
    routing_id: str,
    finding_cluster_id: str,
    submission_id: str,
    payload: ReportQualityAssessmentRequest | None = Body(default=None),
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> ReportQualityAssessment:
    """Trusted validator/protocol operation; miner self-scoring is not exposed."""
    get_project_or_404(db, project_id)
    try:
        assessment, _ = assess_report_quality(
            root,
            project_workspace(project_id),
            project_id,
            routing_id,
            finding_cluster_id,
            submission_id,
            payload,
        )
        return assessment
    except ReportQualityNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (
        ReportQualityConflictError,
        ReportQualityEligibilityError,
        ReportQualityLinkageError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ReportQualityStorageError as exc:
        raise HTTPException(
            status_code=500, detail="Stored report quality data is malformed"
        ) from exc


@router.get(
    "/projects/{project_id}/routing/{routing_id}/finding-clusters/{finding_cluster_id}/quality-assessments",
    response_model=ReportQualityAssessmentListResponse,
)
def list_cluster_quality_assessments(
    project_id: str,
    routing_id: str,
    finding_cluster_id: str,
    include_superseded: bool = Query(default=False),
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> ReportQualityAssessmentListResponse:
    get_project_or_404(db, project_id)
    try:
        assessments = list_cluster_report_quality_assessments(
            root,
            project_id,
            routing_id,
            finding_cluster_id,
            include_superseded=include_superseded,
        )
    except ReportQualityNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ReportQualityLinkageError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ReportQualityStorageError as exc:
        raise HTTPException(
            status_code=500, detail="Stored report quality data is malformed"
        ) from exc
    return ReportQualityAssessmentListResponse(
        project_id=project_id,
        routing_id=routing_id,
        finding_cluster_id=finding_cluster_id,
        total=len(assessments),
        assessments=assessments,
    )


@router.get(
    "/projects/{project_id}/routing/{routing_id}/submissions/{submission_id}/quality-assessment",
    response_model=ReportQualityAssessment,
)
def get_submission_quality_assessment(
    project_id: str,
    routing_id: str,
    submission_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> ReportQualityAssessment:
    get_project_or_404(db, project_id)
    try:
        assessment = load_active_report_quality_assessment(
            root, routing_id, submission_id
        )
    except ReportQualityLinkageError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ReportQualityStorageError as exc:
        raise HTTPException(
            status_code=500, detail="Stored report quality data is malformed"
        ) from exc
    if assessment is None or assessment.project_id != project_id:
        raise HTTPException(status_code=404, detail="Report quality assessment not found")
    return assessment


@router.get(
    "/projects/{project_id}/routing/{routing_id}/quality-assessments/{assessment_id}",
    response_model=ReportQualityAssessment,
)
def get_quality_assessment_by_id(
    project_id: str,
    routing_id: str,
    assessment_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> ReportQualityAssessment:
    """Read one immutable assessment version by its server-derived identity."""
    get_project_or_404(db, project_id)
    try:
        assessment = load_report_quality_assessment_by_id(
            root, project_id, routing_id, assessment_id
        )
    except ReportQualityLinkageError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ReportQualityStorageError as exc:
        raise HTTPException(
            status_code=500, detail="Stored report quality data is malformed"
        ) from exc
    if assessment is None:
        raise HTTPException(status_code=404, detail="Report quality assessment not found")
    return assessment


@router.post(
    "/projects/{project_id}/routing/{routing_id}/quality-assessments/rebuild",
    response_model=ReportQualityRebuildResult,
)
def rebuild_task_quality_assessments(
    project_id: str,
    routing_id: str,
    payload: ReportQualityRebuildRequest | None = Body(default=None),
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> ReportQualityRebuildResult:
    get_project_or_404(db, project_id)
    request = payload or ReportQualityRebuildRequest()
    try:
        return rebuild_task_report_quality_assessments(
            root,
            project_workspace(project_id),
            project_id,
            routing_id,
            supersede_changed_finalized=request.supersede_changed_finalized,
        )
    except ReportQualityNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (
        ReportQualityConflictError,
        ReportQualityEligibilityError,
        ReportQualityLinkageError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ReportQualityStorageError as exc:
        raise HTTPException(
            status_code=500, detail="Stored report quality data is malformed"
        ) from exc
