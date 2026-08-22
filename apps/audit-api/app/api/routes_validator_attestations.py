from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.paths import protocol_data_root
from app.schemas.validator_attestation import (
    ValidationAttestation,
    ValidationAttestationListResponse,
    ValidationAttestationRequest,
    ValidatorAssignment,
    ValidatorAssignmentListResponse,
    ValidatorReproductionRecord,
    ValidatorReproductionRequest,
)
from app.schemas.validator_reproduction import ValidatorReproductionExecutionRequest
from app.services.project_service import get_project_or_404, project_workspace
from app.services.finding_cluster_service import load_finding_cluster
from app.services.validation_attestation_service import (
    create_validation_attestation,
    list_validation_attestations,
    load_validation_attestation,
)
from app.services.validator_assignment_service import (
    ValidatorProtocolConflictError,
    ValidatorProtocolEligibilityError,
    ValidatorProtocolNotFoundError,
    ValidatorProtocolRelationshipError,
    ValidatorProtocolStorageError,
    list_validator_assignments,
    load_validator_assignment,
)
from app.services.validator_reproduction_service import (
    create_validator_reproduction_record,
    list_validator_reproduction_records,
    load_validator_reproduction_record,
)
from app.services.validator_reproduction_execution_service import (
    execute_validator_reproduction,
)


router = APIRouter(tags=["validator-attestations"])


@router.get(
    "/projects/{project_id}/routing/{routing_id}/validator-assignments/{assignment_id}",
    response_model=ValidatorAssignment,
)
def get_validator_assignment(
    project_id: str,
    routing_id: str,
    assignment_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> ValidatorAssignment:
    get_project_or_404(db, project_id)
    try:
        assignment = load_validator_assignment(root, project_id, routing_id, assignment_id)
    except Exception as exc:
        _raise_protocol_http(exc)
    if assignment is None:
        raise HTTPException(status_code=404, detail="Validator assignment not found")
    return assignment


@router.get(
    "/projects/{project_id}/routing/{routing_id}/finding-clusters/{cluster_id}/validator-assignments",
    response_model=ValidatorAssignmentListResponse,
)
def get_cluster_validator_assignments(
    project_id: str,
    routing_id: str,
    cluster_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> ValidatorAssignmentListResponse:
    get_project_or_404(db, project_id)
    try:
        _require_cluster(root, project_id, routing_id, cluster_id)
        assignments = list_validator_assignments(
            root,
            project_id,
            routing_id,
            finding_cluster_id=cluster_id,
        )
    except Exception as exc:
        _raise_protocol_http(exc)
    return ValidatorAssignmentListResponse(
        total=len(assignments), assignments=assignments
    )


@router.post(
    "/projects/{project_id}/routing/{routing_id}/validator-assignments/{assignment_id}/reproduction",
    response_model=ValidatorReproductionRecord,
)
def record_validator_reproduction(
    project_id: str,
    routing_id: str,
    assignment_id: str,
    request: ValidatorReproductionExecutionRequest | ValidatorReproductionRequest,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> ValidatorReproductionRecord:
    get_project_or_404(db, project_id)
    try:
        if isinstance(request, ValidatorReproductionExecutionRequest):
            return execute_validator_reproduction(
                root,
                project_workspace(project_id),
                project_id=project_id,
                routing_id=routing_id,
                validator_assignment_id=assignment_id,
                request=request,
            )
        return create_validator_reproduction_record(
            root,
            project_workspace(project_id),
            project_id=project_id,
            routing_id=routing_id,
            validator_assignment_id=assignment_id,
            request=request,
        )
    except Exception as exc:
        _raise_protocol_http(exc)


@router.get(
    "/projects/{project_id}/routing/{routing_id}/validator-assignments/{assignment_id}/reproduction",
    response_model=ValidatorReproductionRecord,
)
def get_assignment_validator_reproduction(
    project_id: str,
    routing_id: str,
    assignment_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> ValidatorReproductionRecord:
    get_project_or_404(db, project_id)
    try:
        assignment = load_validator_assignment(root, project_id, routing_id, assignment_id)
        if assignment is None:
            raise ValidatorProtocolNotFoundError("Validator assignment not found")
        records = [
            item
            for item in list_validator_reproduction_records(root, project_id, routing_id)
            if item.validator_assignment_id == assignment_id
        ]
    except Exception as exc:
        _raise_protocol_http(exc)
    if not records:
        raise HTTPException(status_code=404, detail="Validator reproduction not found")
    if len(records) != 1:
        raise HTTPException(
            status_code=409,
            detail="Assignment has multiple legacy reproduction records",
        )
    return records[0]


@router.get(
    "/projects/{project_id}/routing/{routing_id}/validator-reproductions/{reproduction_id}",
    response_model=ValidatorReproductionRecord,
)
def get_validator_reproduction(
    project_id: str,
    routing_id: str,
    reproduction_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> ValidatorReproductionRecord:
    get_project_or_404(db, project_id)
    try:
        reproduction = load_validator_reproduction_record(
            root, project_id, routing_id, reproduction_id
        )
    except Exception as exc:
        _raise_protocol_http(exc)
    if reproduction is None:
        raise HTTPException(status_code=404, detail="Validator reproduction not found")
    return reproduction


@router.post(
    "/projects/{project_id}/routing/{routing_id}/validator-assignments/{assignment_id}/attestations",
    response_model=ValidationAttestation,
)
def submit_validation_attestation(
    project_id: str,
    routing_id: str,
    assignment_id: str,
    request: ValidationAttestationRequest,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> ValidationAttestation:
    get_project_or_404(db, project_id)
    try:
        return create_validation_attestation(
            root,
            project_workspace(project_id),
            project_id=project_id,
            routing_id=routing_id,
            validator_assignment_id=assignment_id,
            request=request,
        )
    except Exception as exc:
        _raise_protocol_http(exc)


@router.get(
    "/projects/{project_id}/routing/{routing_id}/attestations/{attestation_id}",
    response_model=ValidationAttestation,
)
def get_validation_attestation(
    project_id: str,
    routing_id: str,
    attestation_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> ValidationAttestation:
    get_project_or_404(db, project_id)
    try:
        attestation = load_validation_attestation(
            root, project_id, routing_id, attestation_id
        )
    except Exception as exc:
        _raise_protocol_http(exc)
    if attestation is None:
        raise HTTPException(status_code=404, detail="Validation attestation not found")
    return attestation


@router.get(
    "/projects/{project_id}/routing/{routing_id}/finding-clusters/{cluster_id}/attestations",
    response_model=ValidationAttestationListResponse,
)
def get_cluster_validation_attestations(
    project_id: str,
    routing_id: str,
    cluster_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> ValidationAttestationListResponse:
    get_project_or_404(db, project_id)
    try:
        _require_cluster(root, project_id, routing_id, cluster_id)
        attestations = list_validation_attestations(
            root,
            project_id,
            routing_id,
            finding_cluster_id=cluster_id,
        )
    except Exception as exc:
        _raise_protocol_http(exc)
    return ValidationAttestationListResponse(
        total=len(attestations), attestations=attestations
    )


def _raise_protocol_http(exc: Exception) -> None:
    if isinstance(exc, ValidatorProtocolNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, ValidatorProtocolEligibilityError):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if isinstance(
        exc,
        (ValidatorProtocolConflictError, ValidatorProtocolRelationshipError),
    ):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, ValidatorProtocolStorageError):
        raise HTTPException(
            status_code=500, detail="Stored validator protocol data is malformed"
        ) from exc
    raise exc


def _require_cluster(
    root: Path,
    project_id: str,
    routing_id: str,
    cluster_id: str,
) -> None:
    try:
        cluster = load_finding_cluster(root, project_id, routing_id, cluster_id)
    except ValueError as exc:
        raise ValidatorProtocolRelationshipError(str(exc)) from exc
    if cluster is None:
        raise ValidatorProtocolNotFoundError("Finding cluster not found")
