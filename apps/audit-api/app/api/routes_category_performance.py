from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.paths import protocol_data_root
from app.schemas.category_performance import (
    CategoryPerformanceRebuildRequest,
    CategoryPerformanceRecord,
)
from app.services.category_performance_service import (
    CategoryPerformanceCategoryMismatchError,
    CategoryPerformanceInputMismatchError,
    CategoryPerformanceNodeNotFoundError,
    CategoryPerformanceRebuildError,
    CategoryPerformanceSourceNotFoundError,
    CategoryPerformanceUnknownEventTypeError,
    CategoryPerformanceUnsupportedCategoryError,
    InvalidCategoryPerformanceIdentifierError,
    get_node_category_performance,
    get_subnet_category_performance,
    list_category_performance,
    load_category_performance,
    rebuild_all_category_performance,
    rebuild_category_performance,
    rebuild_node_category_performance,
)
from app.services.subnet_registry_service import (
    InvalidSubnetIdentifierError,
    SubnetNotFoundError,
)


router = APIRouter(tags=["category-performance"])


@router.post(
    "/nodes/{node_id}/category-performance/rebuild",
    response_model=list[CategoryPerformanceRecord],
)
def rebuild_node_performance(
    node_id: str,
    payload: CategoryPerformanceRebuildRequest | None = None,
    root: Path = Depends(protocol_data_root),
) -> list[CategoryPerformanceRecord]:
    del payload
    try:
        return rebuild_node_category_performance(root, node_id)
    except CategoryPerformanceNodeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except _CONFLICT_ERRORS as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except _BAD_REQUEST_ERRORS as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/nodes/{node_id}/category-performance/{category}/rebuild",
    response_model=CategoryPerformanceRecord,
)
def rebuild_one_category_performance(
    node_id: str,
    category: str,
    payload: CategoryPerformanceRebuildRequest | None = None,
    root: Path = Depends(protocol_data_root),
) -> CategoryPerformanceRecord:
    del payload
    try:
        return rebuild_category_performance(root, node_id, category)
    except CategoryPerformanceNodeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except _CONFLICT_ERRORS as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except _BAD_REQUEST_ERRORS as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/category-performance/rebuild",
    response_model=list[CategoryPerformanceRecord],
)
def rebuild_all_performance(
    payload: CategoryPerformanceRebuildRequest | None = None,
    root: Path = Depends(protocol_data_root),
) -> list[CategoryPerformanceRecord]:
    del payload
    try:
        return rebuild_all_category_performance(root)
    except _CONFLICT_ERRORS as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except _BAD_REQUEST_ERRORS as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/nodes/{node_id}/category-performance",
    response_model=list[CategoryPerformanceRecord],
)
def list_node_performance(
    node_id: str,
    root: Path = Depends(protocol_data_root),
) -> list[CategoryPerformanceRecord]:
    try:
        return get_node_category_performance(root, node_id)
    except CategoryPerformanceNodeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidCategoryPerformanceIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/nodes/{node_id}/category-performance/{category}",
    response_model=CategoryPerformanceRecord,
)
def read_category_performance(
    node_id: str,
    category: str,
    root: Path = Depends(protocol_data_root),
) -> CategoryPerformanceRecord:
    try:
        record = load_category_performance(root, node_id, category)
    except _BAD_REQUEST_ERRORS as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if record is None:
        raise HTTPException(status_code=404, detail="Category-performance record not found")
    return record


@router.get(
    "/category-performance",
    response_model=list[CategoryPerformanceRecord],
)
def list_global_category_performance(
    node_id: str | None = None,
    category: str | None = None,
    minimum_finalized_submissions: int | None = Query(default=None, ge=0),
    root: Path = Depends(protocol_data_root),
) -> list[CategoryPerformanceRecord]:
    try:
        return list_category_performance(
            root,
            node_id=node_id,
            category=category,
            minimum_finalized_submissions=minimum_finalized_submissions,
        )
    except _BAD_REQUEST_ERRORS as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/subnets/{subnet_id}/category-performance",
    response_model=list[CategoryPerformanceRecord],
)
def list_subnet_performance(
    subnet_id: str,
    root: Path = Depends(protocol_data_root),
) -> list[CategoryPerformanceRecord]:
    try:
        return get_subnet_category_performance(root, subnet_id)
    except SubnetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidSubnetIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


_CONFLICT_ERRORS = (
    CategoryPerformanceInputMismatchError,
    CategoryPerformanceCategoryMismatchError,
    CategoryPerformanceSourceNotFoundError,
    CategoryPerformanceUnknownEventTypeError,
    CategoryPerformanceRebuildError,
)
_BAD_REQUEST_ERRORS = (
    InvalidCategoryPerformanceIdentifierError,
    CategoryPerformanceUnsupportedCategoryError,
)
