from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.paths import protocol_data_root
from app.schemas.validator_consensus import (
    ConsensusVerificationResult,
    ValidationConsensus,
    ValidationConsensusActionRequest,
    ValidationConsensusListResponse,
    ValidationDisputeListResponse,
    ValidationEscalationResult,
)
from app.services.project_service import get_project_or_404, project_workspace
from app.services.validator_assignment_service import (
    ValidatorProtocolConflictError,
    ValidatorProtocolNotFoundError,
    ValidatorProtocolRelationshipError,
    ValidatorProtocolStorageError,
)
from app.services.validator_consensus_service import (
    calculate_validation_consensus,
    escalate_validation_dispute,
    finalize_validation_consensus,
    list_validation_consensus,
    list_validation_disputes,
    load_validation_consensus,
    verify_validation_consensus,
)


router = APIRouter(tags=["validator-consensus"])


@router.post(
    "/projects/{project_id}/routing/{routing_id}/finding-clusters/{cluster_id}/validation-consensus",
    response_model=ValidationConsensus,
    status_code=201,
)
def calculate_cluster_consensus(
    project_id: str, routing_id: str, cluster_id: str,
    payload: ValidationConsensusActionRequest,
    db: Session = Depends(get_db), root: Path = Depends(protocol_data_root),
) -> ValidationConsensus:
    del payload
    get_project_or_404(db, project_id)
    try:
        return calculate_validation_consensus(
            root, project_workspace(project_id), project_id=project_id,
            routing_id=routing_id, finding_cluster_id=cluster_id,
        )
    except Exception as exc:
        _raise_consensus_http(exc)


@router.post(
    "/projects/{project_id}/routing/{routing_id}/validation-consensus/{consensus_id}/finalize",
    response_model=ValidationConsensus,
)
def finalize_cluster_consensus(
    project_id: str, routing_id: str, consensus_id: str,
    payload: ValidationConsensusActionRequest,
    db: Session = Depends(get_db), root: Path = Depends(protocol_data_root),
) -> ValidationConsensus:
    del payload
    get_project_or_404(db, project_id)
    try:
        return finalize_validation_consensus(
            root, project_workspace(project_id), project_id=project_id,
            routing_id=routing_id, validation_consensus_id=consensus_id,
        )
    except Exception as exc:
        _raise_consensus_http(exc)


@router.get(
    "/projects/{project_id}/routing/{routing_id}/validation-consensus/{consensus_id}",
    response_model=ValidationConsensus,
)
def get_consensus(
    project_id: str, routing_id: str, consensus_id: str,
    db: Session = Depends(get_db), root: Path = Depends(protocol_data_root),
) -> ValidationConsensus:
    get_project_or_404(db, project_id)
    try:
        result = load_validation_consensus(root, project_id, routing_id, consensus_id)
    except Exception as exc:
        _raise_consensus_http(exc)
    if result is None:
        raise HTTPException(status_code=404, detail="Validation consensus not found")
    return result


@router.get(
    "/projects/{project_id}/routing/{routing_id}/finding-clusters/{cluster_id}/validation-consensus",
    response_model=ValidationConsensusListResponse,
)
def get_cluster_consensus(
    project_id: str, routing_id: str, cluster_id: str,
    db: Session = Depends(get_db), root: Path = Depends(protocol_data_root),
) -> ValidationConsensusListResponse:
    get_project_or_404(db, project_id)
    try:
        values = list_validation_consensus(root, project_id, routing_id, finding_cluster_id=cluster_id)
    except Exception as exc:
        _raise_consensus_http(exc)
    return ValidationConsensusListResponse(total=len(values), consensus=values)


@router.get(
    "/projects/{project_id}/routing/{routing_id}/validation-consensus/{consensus_id}/verify",
    response_model=ConsensusVerificationResult,
)
def verify_consensus(
    project_id: str, routing_id: str, consensus_id: str,
    db: Session = Depends(get_db), root: Path = Depends(protocol_data_root),
) -> ConsensusVerificationResult:
    get_project_or_404(db, project_id)
    try:
        return verify_validation_consensus(
            root, project_workspace(project_id), project_id=project_id,
            routing_id=routing_id, validation_consensus_id=consensus_id,
        )
    except Exception as exc:
        _raise_consensus_http(exc)


@router.get(
    "/projects/{project_id}/routing/{routing_id}/finding-clusters/{cluster_id}/validation-disputes",
    response_model=ValidationDisputeListResponse,
)
def get_cluster_disputes(
    project_id: str, routing_id: str, cluster_id: str,
    db: Session = Depends(get_db), root: Path = Depends(protocol_data_root),
) -> ValidationDisputeListResponse:
    get_project_or_404(db, project_id)
    try:
        values = list_validation_disputes(root, project_id, routing_id, finding_cluster_id=cluster_id)
    except Exception as exc:
        _raise_consensus_http(exc)
    return ValidationDisputeListResponse(total=len(values), disputes=values)


@router.post(
    "/projects/{project_id}/routing/{routing_id}/validation-disputes/{dispute_id}/escalate",
    response_model=ValidationEscalationResult,
)
def escalate_dispute(
    project_id: str, routing_id: str, dispute_id: str,
    payload: ValidationConsensusActionRequest,
    db: Session = Depends(get_db), root: Path = Depends(protocol_data_root),
) -> ValidationEscalationResult:
    del payload
    get_project_or_404(db, project_id)
    try:
        return escalate_validation_dispute(
            root, project_id=project_id, routing_id=routing_id,
            validation_dispute_id=dispute_id,
        )
    except Exception as exc:
        _raise_consensus_http(exc)


def _raise_consensus_http(exc: Exception) -> None:
    if isinstance(exc, ValidatorProtocolNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, (ValidatorProtocolConflictError, ValidatorProtocolRelationshipError)):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, ValidatorProtocolStorageError):
        raise HTTPException(status_code=500, detail="Stored validator consensus data is malformed") from exc
    raise exc
