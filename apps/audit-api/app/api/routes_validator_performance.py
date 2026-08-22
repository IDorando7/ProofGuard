from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.paths import protocol_data_root
from app.schemas.validator_performance import (
    ValidationQualityAssessment,
    ValidationQualityAssessmentListResponse,
    ValidatorCategoryPerformance,
    ValidatorCategoryScore,
    ValidatorMembership,
    ValidatorPerformanceEvaluationRequest,
    ValidatorPerformanceEvaluationResult,
    ValidatorPerformanceStateResult,
)
from app.services.project_service import get_project_or_404, project_workspace
from app.services.validation_quality_assessment_service import (
    ValidationQualityError,
    list_validation_quality_assessments,
    load_validation_quality_assessment,
)
from app.services.validator_assignment_service import (
    ValidatorProtocolConflictError,
    ValidatorProtocolNotFoundError,
    ValidatorProtocolRelationshipError,
    ValidatorProtocolStorageError,
)
from app.services.validator_category_performance_service import (
    load_validator_category_performance,
)
from app.services.validator_category_score_service import load_validator_category_score
from app.services.validator_membership_service import load_validator_membership
from app.services.validator_performance_service import (
    evaluate_resolved_consensus_performance,
    rebuild_validator_performance_state,
)


router = APIRouter(tags=["validator-performance"])


@router.post(
    "/projects/{project_id}/routing/{routing_id}/validation-consensus/{consensus_id}/validator-performance/evaluate",
    response_model=ValidatorPerformanceEvaluationResult,
)
def evaluate_validator_performance(
    project_id: str,
    routing_id: str,
    consensus_id: str,
    payload: ValidatorPerformanceEvaluationRequest,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> ValidatorPerformanceEvaluationResult:
    del payload
    get_project_or_404(db, project_id)
    try:
        return evaluate_resolved_consensus_performance(
            root,
            project_workspace(project_id),
            project_id=project_id,
            routing_id=routing_id,
            final_validation_consensus_id=consensus_id,
        )
    except Exception as exc:
        _raise_performance_http(exc)


@router.get(
    "/projects/{project_id}/routing/{routing_id}/validation-quality-assessments/{assessment_id}",
    response_model=ValidationQualityAssessment,
)
def get_quality_assessment(
    project_id: str,
    routing_id: str,
    assessment_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> ValidationQualityAssessment:
    get_project_or_404(db, project_id)
    try:
        value = load_validation_quality_assessment(root, project_id, routing_id, assessment_id)
    except Exception as exc:
        _raise_performance_http(exc)
    if value is None:
        raise HTTPException(status_code=404, detail="Validation quality assessment not found")
    return value


@router.get(
    "/projects/{project_id}/routing/{routing_id}/finding-clusters/{cluster_id}/validation-quality-assessments",
    response_model=ValidationQualityAssessmentListResponse,
)
def get_cluster_quality_assessments(
    project_id: str,
    routing_id: str,
    cluster_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> ValidationQualityAssessmentListResponse:
    get_project_or_404(db, project_id)
    try:
        values = list_validation_quality_assessments(
            root, project_id, routing_id, finding_cluster_id=cluster_id
        )
    except Exception as exc:
        _raise_performance_http(exc)
    return ValidationQualityAssessmentListResponse(total=len(values), assessments=values)


@router.get(
    "/validators/{validator_node_id}/categories/{category}/performance",
    response_model=ValidatorCategoryPerformance,
)
def get_validator_category_performance(
    validator_node_id: str,
    category: str,
    root: Path = Depends(protocol_data_root),
) -> ValidatorCategoryPerformance:
    try:
        value = load_validator_category_performance(root, validator_node_id, category)
    except Exception as exc:
        _raise_performance_http(exc)
    if value is None:
        raise HTTPException(status_code=404, detail="Validator category performance not found")
    return value


@router.get(
    "/validators/{validator_node_id}/categories/{category}/score",
    response_model=ValidatorCategoryScore,
)
def get_validator_category_score(
    validator_node_id: str,
    category: str,
    root: Path = Depends(protocol_data_root),
) -> ValidatorCategoryScore:
    try:
        value = load_validator_category_score(root, validator_node_id, category)
    except Exception as exc:
        _raise_performance_http(exc)
    if value is None:
        raise HTTPException(status_code=404, detail="Validator category score not found")
    return value


@router.get(
    "/validators/{validator_node_id}/categories/{category}/membership",
    response_model=ValidatorMembership,
)
def get_validator_membership_state(
    validator_node_id: str,
    category: str,
    root: Path = Depends(protocol_data_root),
) -> ValidatorMembership:
    try:
        value = load_validator_membership(root, validator_node_id, category)
    except Exception as exc:
        _raise_performance_http(exc)
    if value is None:
        raise HTTPException(status_code=404, detail="Validator membership not found")
    return value


@router.post(
    "/validators/{validator_node_id}/categories/{category}/performance/rebuild",
    response_model=ValidatorPerformanceStateResult,
)
def rebuild_validator_state(
    validator_node_id: str,
    category: str,
    payload: ValidatorPerformanceEvaluationRequest,
    root: Path = Depends(protocol_data_root),
) -> ValidatorPerformanceStateResult:
    del payload
    try:
        performance, score, membership = rebuild_validator_performance_state(
            root, validator_node_id=validator_node_id, category=category
        )
    except Exception as exc:
        _raise_performance_http(exc)
    return ValidatorPerformanceStateResult(
        category_performance=performance,
        category_score=score,
        membership=membership,
    )


def _raise_performance_http(exc: Exception) -> None:
    if isinstance(exc, ValidatorProtocolNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(
        exc,
        (
            ValidationQualityError,
            ValidatorProtocolConflictError,
            ValidatorProtocolRelationshipError,
        ),
    ):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, (ValidatorProtocolStorageError, OSError)):
        raise HTTPException(status_code=500, detail="Validator performance storage failure") from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise exc
