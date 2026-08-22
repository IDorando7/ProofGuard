from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.paths import protocol_data_root
from app.schemas.validator_attestation import (
    ValidatorAssignmentListResponse,
)
from app.schemas.validator_committee import (
    ValidatorCommittee,
    ValidatorCommitteeCreateRequest,
    ValidatorCommitteeFinalizationResponse,
    ValidatorCommitteeFinalizeRequest,
    ValidatorCommitteeListResponse,
)
from app.schemas.validator_reproduction import (
    CommitteeReproductionReadiness,
    ValidatorReproductionListResponse,
)
from app.services.project_service import get_project_or_404
from app.services.finding_cluster_service import load_finding_cluster
from app.services.validator_assignment_service import (
    ValidatorProtocolConflictError,
    ValidatorProtocolEligibilityError,
    ValidatorProtocolNotFoundError,
    ValidatorProtocolRelationshipError,
    ValidatorProtocolStorageError,
    list_validator_assignments,
)
from app.services.validator_committee_service import (
    finalize_validator_committee,
    list_validator_committees,
    load_validator_committee,
    plan_validator_committee,
)
from app.services.validator_reproduction_execution_service import (
    get_committee_reproduction_readiness,
)
from app.services.validator_reproduction_service import (
    list_validator_reproduction_records,
)


router = APIRouter(tags=["validator-committees"])


@router.post(
    "/projects/{project_id}/routing/{routing_id}/finding-clusters/{cluster_id}/validator-committees",
    response_model=ValidatorCommittee,
    status_code=201,
)
def create_standard_validator_committee(
    project_id: str,
    routing_id: str,
    cluster_id: str,
    payload: ValidatorCommitteeCreateRequest,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> ValidatorCommittee:
    del payload
    get_project_or_404(db, project_id)
    try:
        # The unauthenticated public API is deliberately STANDARD-only. Trusted
        # orchestration may request HIGH_ASSURANCE through the service API.
        return plan_validator_committee(
            root,
            project_id=project_id,
            routing_id=routing_id,
            finding_cluster_id=cluster_id,
        )
    except Exception as exc:
        _raise_committee_http(exc)


@router.post(
    "/projects/{project_id}/routing/{routing_id}/validator-committees/{committee_id}/finalize",
    response_model=ValidatorCommitteeFinalizationResponse,
)
def finalize_planned_validator_committee(
    project_id: str,
    routing_id: str,
    committee_id: str,
    payload: ValidatorCommitteeFinalizeRequest,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> ValidatorCommitteeFinalizationResponse:
    del payload
    get_project_or_404(db, project_id)
    try:
        return finalize_validator_committee(
            root,
            project_id=project_id,
            routing_id=routing_id,
            validator_committee_id=committee_id,
        )
    except Exception as exc:
        _raise_committee_http(exc)


@router.get(
    "/projects/{project_id}/routing/{routing_id}/finding-clusters/{cluster_id}/validator-committees",
    response_model=ValidatorCommitteeListResponse,
)
def get_finding_cluster_validator_committees(
    project_id: str,
    routing_id: str,
    cluster_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> ValidatorCommitteeListResponse:
    get_project_or_404(db, project_id)
    try:
        cluster = load_finding_cluster(root, project_id, routing_id, cluster_id)
        if cluster is None:
            raise ValidatorProtocolNotFoundError("Finding cluster not found")
        committees = list_validator_committees(
            root,
            project_id,
            routing_id,
            finding_cluster_id=cluster_id,
        )
    except Exception as exc:
        _raise_committee_http(exc)
    return ValidatorCommitteeListResponse(total=len(committees), committees=committees)


@router.get(
    "/projects/{project_id}/routing/{routing_id}/validator-committees/{committee_id}",
    response_model=ValidatorCommittee,
)
def get_validator_committee(
    project_id: str,
    routing_id: str,
    committee_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> ValidatorCommittee:
    get_project_or_404(db, project_id)
    try:
        committee = load_validator_committee(root, project_id, routing_id, committee_id)
    except Exception as exc:
        _raise_committee_http(exc)
    if committee is None:
        raise HTTPException(status_code=404, detail="Validator committee not found")
    return committee


@router.get(
    "/projects/{project_id}/routing/{routing_id}/validator-committees/{committee_id}/assignments",
    response_model=ValidatorAssignmentListResponse,
)
def get_validator_committee_assignments(
    project_id: str,
    routing_id: str,
    committee_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> ValidatorAssignmentListResponse:
    get_project_or_404(db, project_id)
    try:
        committee = load_validator_committee(root, project_id, routing_id, committee_id)
        if committee is None:
            raise ValidatorProtocolNotFoundError("Validator committee not found")
        assignments = [
            assignment
            for assignment in list_validator_assignments(
                root,
                project_id,
                routing_id,
                finding_cluster_id=committee.finding_cluster_id,
            )
            if assignment.validator_committee_id == committee_id
        ]
        seat_order = {
            seat.validator_assignment_id: (0, seat.seat_index)
            for seat in committee.authoritative_seats
        }
        seat_order.update(
            {
                seat.validator_assignment_id: (1, seat.seat_index)
                for seat in committee.shadow_seats
            }
        )
        assignments.sort(key=lambda item: seat_order[item.validator_assignment_id])
    except Exception as exc:
        _raise_committee_http(exc)
    return ValidatorAssignmentListResponse(total=len(assignments), assignments=assignments)


@router.get(
    "/projects/{project_id}/routing/{routing_id}/validator-committees/{committee_id}/reproductions",
    response_model=ValidatorReproductionListResponse,
)
def get_validator_committee_reproductions(
    project_id: str,
    routing_id: str,
    committee_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> ValidatorReproductionListResponse:
    get_project_or_404(db, project_id)
    try:
        committee = load_validator_committee(root, project_id, routing_id, committee_id)
        if committee is None:
            raise ValidatorProtocolNotFoundError("Validator committee not found")
        records = list_validator_reproduction_records(
            root,
            project_id,
            routing_id,
            validator_committee_id=committee_id,
        )
        seat_order = {
            seat.validator_assignment_id: (0, seat.seat_index)
            for seat in committee.authoritative_seats
        }
        seat_order.update(
            {
                seat.validator_assignment_id: (1, seat.seat_index)
                for seat in committee.shadow_seats
            }
        )
        records.sort(key=lambda item: seat_order[item.validator_assignment_id])
    except Exception as exc:
        _raise_committee_http(exc)
    return ValidatorReproductionListResponse(total=len(records), reproductions=records)


@router.get(
    "/projects/{project_id}/routing/{routing_id}/validator-committees/{committee_id}/reproduction-readiness",
    response_model=CommitteeReproductionReadiness,
)
def get_validator_committee_reproduction_readiness(
    project_id: str,
    routing_id: str,
    committee_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> CommitteeReproductionReadiness:
    get_project_or_404(db, project_id)
    try:
        return get_committee_reproduction_readiness(
            root,
            project_id=project_id,
            routing_id=routing_id,
            validator_committee_id=committee_id,
        )
    except Exception as exc:
        _raise_committee_http(exc)


def _raise_committee_http(exc: Exception) -> None:
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
            status_code=500, detail="Stored validator committee data is malformed"
        ) from exc
    raise exc
