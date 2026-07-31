from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.paths import protocol_data_root
from app.schemas.routing import (
    ProjectRoutingListResponse,
    ProjectRoutingRecord,
    ProjectRoutingRequest,
    RoutingCalculationResponse,
    RoutingFinalizeRequest,
    RoutingFinalizationResponse,
    RoutingProcessingStatus,
    RoutingSelectionType,
    RoutingStatus,
    RoutingUsageEvent,
)
from app.services.node_registry_service import InvalidNodeIdentifierError, load_node
from app.services.project_service import get_project_or_404, project_workspace
from app.services.subnet_registry_service import InvalidSubnetIdentifierError, load_subnet
from app.services.subnet_router_service import (
    InvalidRoutingIdentifierError,
    RoutingCategoryNotInScopeError,
    RoutingIncompleteError,
    RoutingInputMismatchError,
    RoutingProjectCategoryMissingError,
    RoutingProjectNotFoundError,
    RoutingProjectScopeMissingError,
    RoutingRecordNotFoundError,
    RoutingSourceChangedError,
    RoutingStorageError,
    RoutingSupersededError,
    RoutingUsageConflictError,
    calculate_project_routing,
    finalize_project_routing,
    list_project_routing_records,
    list_routing_usage_events,
    load_routing_record,
)


router = APIRouter(tags=["routing"])


@router.post(
    "/projects/{project_id}/routing/calculate",
    response_model=RoutingCalculationResponse,
    status_code=201,
)
def calculate_routing(
    project_id: str,
    payload: ProjectRoutingRequest,
    response: Response,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> RoutingCalculationResponse:
    get_project_or_404(db, project_id)
    try:
        result = calculate_project_routing(
            root,
            project_workspace(project_id),
            project_id,
            payload,
        )
    except RoutingProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (
        RoutingProjectScopeMissingError,
        RoutingProjectCategoryMissingError,
        RoutingCategoryNotInScopeError,
        RoutingInputMismatchError,
        InvalidRoutingIdentifierError,
    ) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RoutingIncompleteError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RoutingStorageError as exc:
        raise HTTPException(status_code=500, detail="Stored routing data is malformed") from exc
    if result.status == RoutingProcessingStatus.UNCHANGED:
        response.status_code = 200
    return result


@router.post(
    "/projects/{project_id}/routing/{routing_id}/finalize",
    response_model=RoutingFinalizationResponse,
)
def finalize_routing(
    project_id: str,
    routing_id: str,
    payload: RoutingFinalizeRequest | None = None,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> RoutingFinalizationResponse:
    del payload
    get_project_or_404(db, project_id)
    try:
        return finalize_project_routing(
            root,
            project_workspace(project_id),
            project_id,
            routing_id,
        )
    except RoutingRecordNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (
        RoutingSourceChangedError,
        RoutingSupersededError,
        RoutingUsageConflictError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except InvalidRoutingIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RoutingStorageError as exc:
        raise HTTPException(status_code=500, detail="Stored routing data is malformed") from exc


@router.get(
    "/projects/{project_id}/routing/latest",
    response_model=ProjectRoutingRecord,
)
def get_latest_routing(
    project_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> ProjectRoutingRecord:
    get_project_or_404(db, project_id)
    try:
        records = list_project_routing_records(root, project_id)
    except InvalidRoutingIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finalized = next(
        (record for record in records if record.status == RoutingStatus.FINALIZED),
        None,
    )
    latest = finalized or next(
        (record for record in records if record.status == RoutingStatus.CALCULATED),
        None,
    )
    if latest is None:
        raise HTTPException(status_code=404, detail="Routing record not found")
    return latest


@router.get(
    "/projects/{project_id}/routing/{routing_id}",
    response_model=ProjectRoutingRecord,
)
def get_routing(
    project_id: str,
    routing_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> ProjectRoutingRecord:
    get_project_or_404(db, project_id)
    try:
        record = load_routing_record(root, project_id, routing_id)
    except InvalidRoutingIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if record is None:
        raise HTTPException(status_code=404, detail="Routing record not found")
    return record


@router.get(
    "/projects/{project_id}/routing",
    response_model=ProjectRoutingListResponse,
)
def list_project_routing(
    project_id: str,
    status: RoutingStatus | None = None,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> ProjectRoutingListResponse:
    get_project_or_404(db, project_id)
    try:
        records = list_project_routing_records(root, project_id, status=status)
    except InvalidRoutingIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ProjectRoutingListResponse(
        project_id=project_id,
        total=len(records),
        records=records,
    )


@router.get(
    "/nodes/{node_id}/routing-usage",
    response_model=list[RoutingUsageEvent],
)
def list_node_routing_usage(
    node_id: str,
    category: str | None = None,
    selection_type: RoutingSelectionType | None = None,
    root: Path = Depends(protocol_data_root),
) -> list[RoutingUsageEvent]:
    try:
        if load_node(root, node_id) is None:
            raise HTTPException(status_code=404, detail="Node not found")
        return list_routing_usage_events(
            root,
            node_id=node_id,
            category=category,
            selection_type=selection_type,
        )
    except (InvalidRoutingIdentifierError, InvalidNodeIdentifierError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RoutingInputMismatchError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/subnets/{subnet_id}/routing-usage",
    response_model=list[RoutingUsageEvent],
)
def list_subnet_routing_usage(
    subnet_id: str,
    root: Path = Depends(protocol_data_root),
) -> list[RoutingUsageEvent]:
    try:
        if load_subnet(root, subnet_id) is None:
            raise HTTPException(status_code=404, detail="Subnet not found")
        return list_routing_usage_events(root, subnet_id=subnet_id)
    except (InvalidRoutingIdentifierError, InvalidSubnetIdentifierError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
