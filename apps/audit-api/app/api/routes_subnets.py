from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status as http_status

from app.core.paths import protocol_data_root
from app.schemas.subnet import (
    SubnetCreate,
    SubnetMemberListResponse,
    SubnetMemberRecord,
    SubnetMemberStatus,
    SubnetRecord,
    SubnetStatus,
    SubnetStatusChangeRequest,
    SubnetUpdate,
)
from app.services.subnet_registry_service import (
    InvalidSubnetIdentifierError,
    InvalidSubnetStatusTransitionError,
    SubnetArchivedError,
    SubnetCategoryAlreadyRegisteredError,
    SubnetMemberCategoryMismatchError,
    SubnetNotFoundError,
    SubnetRegistryError,
    UnsupportedSubnetCategoryError,
    bootstrap_default_subnets,
    change_subnet_status,
    create_subnet as register_subnet,
    find_subnet_by_category,
    list_subnet_members,
    list_subnets,
    load_subnet,
    load_subnet_member,
    update_subnet,
)


router = APIRouter(prefix="/subnets", tags=["subnets"])


@router.post("", response_model=SubnetRecord, status_code=http_status.HTTP_201_CREATED)
def create_subnet(
    payload: SubnetCreate,
    root: Path = Depends(protocol_data_root),
) -> SubnetRecord:
    try:
        return register_subnet(root, payload)
    except SubnetCategoryAlreadyRegisteredError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except UnsupportedSubnetCategoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/bootstrap", response_model=list[SubnetRecord])
def bootstrap_subnets(root: Path = Depends(protocol_data_root)) -> list[SubnetRecord]:
    return bootstrap_default_subnets(root)


@router.get("", response_model=list[SubnetRecord])
def get_subnets(
    category: str | None = None,
    status: SubnetStatus | None = None,
    root: Path = Depends(protocol_data_root),
) -> list[SubnetRecord]:
    try:
        return list_subnets(root, category=category, status=status)
    except UnsupportedSubnetCategoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/by-category/{category}", response_model=SubnetRecord)
def get_subnet_by_category(
    category: str,
    root: Path = Depends(protocol_data_root),
) -> SubnetRecord:
    try:
        subnet = find_subnet_by_category(root, category)
    except UnsupportedSubnetCategoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if subnet is None:
        raise HTTPException(status_code=404, detail="Subnet not found")
    return subnet


@router.get("/{subnet_id}", response_model=SubnetRecord)
def get_subnet(
    subnet_id: str,
    root: Path = Depends(protocol_data_root),
) -> SubnetRecord:
    try:
        subnet = load_subnet(root, subnet_id)
    except InvalidSubnetIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if subnet is None:
        raise HTTPException(status_code=404, detail="Subnet not found")
    return subnet


@router.patch("/{subnet_id}", response_model=SubnetRecord)
def patch_subnet(
    subnet_id: str,
    payload: SubnetUpdate,
    root: Path = Depends(protocol_data_root),
) -> SubnetRecord:
    try:
        return update_subnet(root, subnet_id, payload)
    except SubnetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SubnetArchivedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (InvalidSubnetIdentifierError, SubnetRegistryError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{subnet_id}/status", response_model=SubnetRecord)
def set_subnet_status(
    subnet_id: str,
    payload: SubnetStatusChangeRequest,
    root: Path = Depends(protocol_data_root),
) -> SubnetRecord:
    try:
        return change_subnet_status(root, subnet_id, payload)
    except SubnetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidSubnetStatusTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except InvalidSubnetIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{subnet_id}/members", response_model=SubnetMemberListResponse)
def get_subnet_members(
    subnet_id: str,
    status: SubnetMemberStatus | None = None,
    minimum_category_score: float | None = Query(default=None, ge=0.0, le=1.0),
    minimum_finalized_submissions: int | None = Query(default=None, ge=0),
    root: Path = Depends(protocol_data_root),
) -> SubnetMemberListResponse:
    try:
        subnet = load_subnet(root, subnet_id)
        if subnet is None:
            raise SubnetNotFoundError("Subnet not found")
        members = list_subnet_members(
            root,
            subnet_id,
            status=status,
            minimum_category_score=minimum_category_score,
            minimum_finalized_submissions=minimum_finalized_submissions,
        )
    except SubnetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidSubnetIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SubnetMemberListResponse(
        subnet_id=subnet.subnet_id,
        category=subnet.category,
        total=len(members),
        members=members,
    )


@router.get("/{subnet_id}/members/{node_id}", response_model=SubnetMemberRecord)
def get_subnet_member(
    subnet_id: str,
    node_id: str,
    root: Path = Depends(protocol_data_root),
) -> SubnetMemberRecord:
    try:
        member = load_subnet_member(root, subnet_id, node_id)
    except SubnetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (InvalidSubnetIdentifierError, SubnetMemberCategoryMismatchError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if member is None:
        raise HTTPException(status_code=404, detail="Subnet member not found")
    return member
