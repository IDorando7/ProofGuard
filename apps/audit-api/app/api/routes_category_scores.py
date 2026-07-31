from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.paths import protocol_data_root
from app.schemas.category_score import (
    CategoryScoreBand,
    CategoryScoreBatchResult,
    CategoryScoreRebuildRequest,
    CategoryScoreRebuildResult,
    CategoryScoreRecord,
)
from app.services.category_scoring_service import (
    CategoryScoreCalculationError,
    CategoryScoreInputMismatchError,
    CategoryScoreNodeNotFoundError,
    CategoryScorePerformanceNotFoundError,
    CategoryScoreSourceChangedError,
    CategoryScoreUnsupportedCategoryError,
    InvalidCategoryScoreIdentifierError,
    get_node_category_scores,
    list_category_scores,
    list_scores_for_subnet,
    load_category_score,
    rebuild_all_category_scores,
    rebuild_category_score,
    rebuild_node_category_scores,
)
from app.services.subnet_registry_service import (
    InvalidSubnetIdentifierError,
    SubnetNotFoundError,
)


router = APIRouter(tags=["category-scores"])


@router.post(
    "/nodes/{node_id}/category-scores/rebuild",
    response_model=list[CategoryScoreRebuildResult],
)
def rebuild_node_scores(
    node_id: str,
    payload: CategoryScoreRebuildRequest | None = None,
    rebuild_performance: bool = Query(default=False),
    root: Path = Depends(protocol_data_root),
) -> list[CategoryScoreRebuildResult]:
    del payload
    try:
        return rebuild_node_category_scores(
            root,
            node_id,
            rebuild_performance=rebuild_performance,
        )
    except CategoryScoreNodeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except _CONFLICT_ERRORS as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except _BAD_REQUEST_ERRORS as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/nodes/{node_id}/category-scores/{category}/rebuild",
    response_model=CategoryScoreRebuildResult,
)
def rebuild_one_score(
    node_id: str,
    category: str,
    payload: CategoryScoreRebuildRequest | None = None,
    rebuild_performance: bool = Query(default=False),
    root: Path = Depends(protocol_data_root),
) -> CategoryScoreRebuildResult:
    del payload
    try:
        return rebuild_category_score(
            root,
            node_id,
            category,
            rebuild_performance=rebuild_performance,
        )
    except (
        CategoryScoreNodeNotFoundError,
        CategoryScorePerformanceNotFoundError,
    ) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except _CONFLICT_ERRORS as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except _BAD_REQUEST_ERRORS as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/category-scores/rebuild",
    response_model=CategoryScoreBatchResult,
)
def rebuild_all_scores(
    payload: CategoryScoreRebuildRequest | None = None,
    rebuild_performance: bool = Query(default=False),
    root: Path = Depends(protocol_data_root),
) -> CategoryScoreBatchResult:
    del payload
    try:
        return rebuild_all_category_scores(
            root,
            rebuild_performance=rebuild_performance,
        )
    except _CONFLICT_ERRORS as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except _BAD_REQUEST_ERRORS as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/nodes/{node_id}/category-scores",
    response_model=list[CategoryScoreRecord],
)
def list_node_scores(
    node_id: str,
    root: Path = Depends(protocol_data_root),
) -> list[CategoryScoreRecord]:
    try:
        return get_node_category_scores(root, node_id)
    except CategoryScoreNodeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except _BAD_REQUEST_ERRORS as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/nodes/{node_id}/category-scores/{category}",
    response_model=CategoryScoreRecord,
)
def read_one_score(
    node_id: str,
    category: str,
    root: Path = Depends(protocol_data_root),
) -> CategoryScoreRecord:
    try:
        record = load_category_score(root, node_id, category)
    except _BAD_REQUEST_ERRORS as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if record is None:
        raise HTTPException(status_code=404, detail="Category score not found")
    return record


@router.get(
    "/category-scores",
    response_model=list[CategoryScoreRecord],
)
def list_global_scores(
    node_id: str | None = None,
    category: str | None = None,
    minimum_score: float | None = Query(default=None, ge=0, le=1),
    minimum_confidence: float | None = Query(default=None, ge=0, le=1),
    score_band: CategoryScoreBand | None = None,
    root: Path = Depends(protocol_data_root),
) -> list[CategoryScoreRecord]:
    try:
        return list_category_scores(
            root,
            node_id=node_id,
            category=category,
            minimum_score=minimum_score,
            minimum_confidence=minimum_confidence,
            score_band=score_band,
        )
    except _BAD_REQUEST_ERRORS as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/subnets/{subnet_id}/category-scores",
    response_model=list[CategoryScoreRecord],
)
def list_subnet_scores(
    subnet_id: str,
    root: Path = Depends(protocol_data_root),
) -> list[CategoryScoreRecord]:
    try:
        return list_scores_for_subnet(root, subnet_id)
    except SubnetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidSubnetIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except _BAD_REQUEST_ERRORS as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


_CONFLICT_ERRORS = (
    CategoryScoreInputMismatchError,
    CategoryScoreSourceChangedError,
)
_BAD_REQUEST_ERRORS = (
    InvalidCategoryScoreIdentifierError,
    CategoryScoreUnsupportedCategoryError,
    CategoryScoreCalculationError,
)
