from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from app.core.paths import protocol_data_root
from app.schemas.subnet_membership import (
    MembershipAdministrativeActionRequest,
    MembershipEvaluationResult,
    MembershipRefreshRequest,
    SubnetMembershipDecision,
    SubnetMembershipRefreshResult,
)
from app.services.subnet_membership_service import (
    InvalidSubnetMembershipIdentifierError,
    SubnetMembershipCalculationError,
    SubnetMembershipMemberNotFoundError,
    SubnetMembershipNodeNotFoundError,
    SubnetMembershipPerformanceNotFoundError,
    SubnetMembershipScoreNotFoundError,
    SubnetMembershipSourceMismatchError,
    SubnetMembershipStorageError,
    SubnetMembershipSubnetNotFoundError,
    administratively_remove_member,
    administratively_suspend_member,
    evaluate_node_membership,
    list_membership_events,
    refresh_subnet_memberships,
)
from app.services.subnet_registry_service import (
    InvalidSubnetIdentifierError,
    SubnetMemberCategoryMismatchError,
    SubnetMemberNotFoundError,
    SubnetNotFoundError,
    load_subnet_member,
)


router = APIRouter(prefix="/subnets", tags=["subnet-membership"])


@router.post(
    "/{subnet_id}/members/refresh",
    response_model=SubnetMembershipRefreshResult,
)
def refresh_memberships(
    subnet_id: str,
    rebuild_dependencies: bool = False,
    payload: MembershipRefreshRequest | None = None,
    root: Path = Depends(protocol_data_root),
) -> SubnetMembershipRefreshResult:
    del payload
    try:
        return refresh_subnet_memberships(
            root,
            subnet_id,
            rebuild_dependencies=rebuild_dependencies,
        )
    except SubnetMembershipSubnetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidSubnetMembershipIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SubnetMembershipSourceMismatchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SubnetMembershipStorageError as exc:
        raise HTTPException(status_code=500, detail="Stored membership data is malformed") from exc


@router.post(
    "/{subnet_id}/members/{node_id}/evaluate",
    response_model=MembershipEvaluationResult,
)
def evaluate_member(
    subnet_id: str,
    node_id: str,
    rebuild_dependencies: bool = False,
    payload: MembershipRefreshRequest | None = None,
    root: Path = Depends(protocol_data_root),
) -> MembershipEvaluationResult:
    del payload
    try:
        return evaluate_node_membership(
            root,
            subnet_id,
            node_id,
            rebuild_dependencies=rebuild_dependencies,
        )
    except (
        SubnetMembershipSubnetNotFoundError,
        SubnetMembershipNodeNotFoundError,
    ) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (
        SubnetMembershipPerformanceNotFoundError,
        SubnetMembershipScoreNotFoundError,
        SubnetMembershipSourceMismatchError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except InvalidSubnetMembershipIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SubnetMembershipCalculationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get(
    "/{subnet_id}/members/{node_id}/history",
    response_model=list[SubnetMembershipDecision],
)
def get_member_history(
    subnet_id: str,
    node_id: str,
    root: Path = Depends(protocol_data_root),
) -> list[SubnetMembershipDecision]:
    try:
        member = load_subnet_member(root, subnet_id, node_id)
        if member is None:
            raise SubnetMembershipMemberNotFoundError("Subnet member not found")
        return list_membership_events(root, subnet_id, node_id)
    except (
        SubnetMembershipSubnetNotFoundError,
        SubnetMembershipMemberNotFoundError,
        SubnetNotFoundError,
    ) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (
        InvalidSubnetMembershipIdentifierError,
        InvalidSubnetIdentifierError,
        SubnetMemberCategoryMismatchError,
    ) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/{subnet_id}/members/{node_id}/suspend",
    response_model=MembershipEvaluationResult,
)
def suspend_member(
    subnet_id: str,
    node_id: str,
    payload: MembershipAdministrativeActionRequest,
    root: Path = Depends(protocol_data_root),
) -> MembershipEvaluationResult:
    try:
        return administratively_suspend_member(
            root, subnet_id, node_id, payload.reason
        )
    except (
        SubnetMembershipMemberNotFoundError,
        SubnetMembershipSubnetNotFoundError,
        SubnetMembershipNodeNotFoundError,
        SubnetMemberNotFoundError,
        SubnetNotFoundError,
    ) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidSubnetMembershipIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SubnetMembershipSourceMismatchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/{subnet_id}/members/{node_id}/remove",
    response_model=MembershipEvaluationResult,
)
def remove_member(
    subnet_id: str,
    node_id: str,
    payload: MembershipAdministrativeActionRequest,
    root: Path = Depends(protocol_data_root),
) -> MembershipEvaluationResult:
    try:
        return administratively_remove_member(
            root, subnet_id, node_id, payload.reason
        )
    except (
        SubnetMembershipMemberNotFoundError,
        SubnetMembershipSubnetNotFoundError,
        SubnetMembershipNodeNotFoundError,
        SubnetMemberNotFoundError,
        SubnetNotFoundError,
    ) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidSubnetMembershipIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SubnetMembershipSourceMismatchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
