from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.paths import protocol_data_root
from app.schemas.finding_cluster import (
    FindingCluster,
    FindingClusterFinalizationResult,
    FindingClusterFinalizeRequest,
    FindingClusterListResponse,
    FindingClusterRebuildRequest,
    FindingClusterRebuildResult,
    SubmissionFindingClusterResponse,
)
from app.services.finding_cluster_service import (
    FindingClusterFinalizedError,
    FindingClusterInputError,
    FindingClusterProjectMismatchError,
    FindingClusterRoutingNotFinalizedError,
    FindingClusterRoutingNotFoundError,
    FindingClusterStorageError,
    find_cluster_by_submission,
    finalize_finding_clusters_for_task,
    list_task_finding_clusters,
    load_finding_cluster,
    rebuild_finding_clusters_for_task,
)
from app.services.project_service import get_project_or_404, project_workspace


router = APIRouter(tags=["finding-clusters"])


@router.post(
    "/projects/{project_id}/routing/{routing_id}/finding-clusters/rebuild",
    response_model=FindingClusterRebuildResult,
)
def rebuild_task_finding_clusters(
    project_id: str,
    routing_id: str,
    payload: FindingClusterRebuildRequest | None = Body(default=None),
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> FindingClusterRebuildResult:
    del payload
    get_project_or_404(db, project_id)
    try:
        return rebuild_finding_clusters_for_task(
            root,
            project_workspace(project_id),
            project_id,
            routing_id,
        )
    except FindingClusterRoutingNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (
        FindingClusterRoutingNotFinalizedError,
        FindingClusterProjectMismatchError,
        FindingClusterFinalizedError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except FindingClusterInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FindingClusterStorageError as exc:
        raise HTTPException(
            status_code=500, detail="Stored finding cluster data is malformed"
        ) from exc


@router.post(
    "/projects/{project_id}/routing/{routing_id}/finding-clusters/finalize",
    response_model=FindingClusterFinalizationResult,
)
def finalize_task_finding_clusters(
    project_id: str,
    routing_id: str,
    payload: FindingClusterFinalizeRequest | None = Body(default=None),
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> FindingClusterFinalizationResult:
    del payload
    get_project_or_404(db, project_id)
    try:
        before = list_task_finding_clusters(root, project_id, routing_id)
        already = sum(
            cluster.status.value == "finalized" for cluster in before
        )
        clusters = finalize_finding_clusters_for_task(
            root, project_id, routing_id
        )
    except FindingClusterProjectMismatchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except FindingClusterInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FindingClusterStorageError as exc:
        raise HTTPException(
            status_code=500, detail="Stored finding cluster data is malformed"
        ) from exc
    return FindingClusterFinalizationResult(
        project_id=project_id,
        routing_id=routing_id,
        finalized_clusters=len(clusters) - already,
        already_finalized_clusters=already,
        clusters=clusters,
    )


@router.get(
    "/projects/{project_id}/routing/{routing_id}/finding-clusters",
    response_model=FindingClusterListResponse,
)
def list_task_clusters(
    project_id: str,
    routing_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> FindingClusterListResponse:
    get_project_or_404(db, project_id)
    try:
        clusters = list_task_finding_clusters(root, project_id, routing_id)
    except FindingClusterProjectMismatchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except FindingClusterInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FindingClusterStorageError as exc:
        raise HTTPException(
            status_code=500, detail="Stored finding cluster data is malformed"
        ) from exc
    return FindingClusterListResponse(
        project_id=project_id,
        routing_id=routing_id,
        total=len(clusters),
        clusters=clusters,
    )


@router.get(
    "/projects/{project_id}/routing/{routing_id}/finding-clusters/{finding_cluster_id}",
    response_model=FindingCluster,
)
def get_task_cluster(
    project_id: str,
    routing_id: str,
    finding_cluster_id: str,
    db: Session = Depends(get_db),
    root: Path = Depends(protocol_data_root),
) -> FindingCluster:
    get_project_or_404(db, project_id)
    try:
        cluster = load_finding_cluster(
            root, project_id, routing_id, finding_cluster_id
        )
    except FindingClusterProjectMismatchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except FindingClusterInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FindingClusterStorageError as exc:
        raise HTTPException(
            status_code=500, detail="Stored finding cluster data is malformed"
        ) from exc
    if cluster is None:
        raise HTTPException(status_code=404, detail="Finding cluster not found")
    return cluster


@router.get(
    "/submissions/{submission_id}/finding-cluster",
    response_model=SubmissionFindingClusterResponse,
)
def get_submission_cluster(
    submission_id: str,
    root: Path = Depends(protocol_data_root),
) -> SubmissionFindingClusterResponse:
    try:
        cluster = find_cluster_by_submission(root, submission_id)
    except FindingClusterInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FindingClusterStorageError as exc:
        raise HTTPException(
            status_code=500, detail="Stored finding cluster data is malformed"
        ) from exc
    if cluster is None:
        raise HTTPException(status_code=404, detail="Submission finding cluster not found")
    return SubmissionFindingClusterResponse(
        submission_id=submission_id,
        finding_cluster=cluster,
    )
