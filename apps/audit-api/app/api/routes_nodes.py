from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status as http_status

from app.core.paths import protocol_data_root
from app.schemas.node import NodeCreate, NodeRecord, NodeStatus, NodeStatusChangeRequest, NodeType, NodeUpdate
from app.services.node_registry_service import (
    DuplicatePublicKeyError,
    InvalidNodeIdentifierError,
    InvalidNodeStatusTransitionError,
    NodeNotFoundError,
    NodeRegistryError,
    UnsupportedNodeCategoryError,
    change_node_status,
    create_node as register_node,
    list_nodes,
    load_node,
    update_node,
)


router = APIRouter(prefix="/nodes", tags=["nodes"])


@router.post("", response_model=NodeRecord, status_code=http_status.HTTP_201_CREATED)
def create_node(payload: NodeCreate, root: Path = Depends(protocol_data_root)) -> NodeRecord:
    try:
        return register_node(root, payload)
    except DuplicatePublicKeyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("", response_model=list[NodeRecord])
def get_nodes(
    node_type: NodeType | None = None,
    status: NodeStatus | None = None,
    category: str | None = None,
    operator_id: str | None = None,
    root: Path = Depends(protocol_data_root),
) -> list[NodeRecord]:
    try:
        return list_nodes(
            root,
            node_type=node_type,
            status=status,
            category=category,
            operator_id=operator_id,
        )
    except UnsupportedNodeCategoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{node_id}", response_model=NodeRecord)
def get_node(node_id: str, root: Path = Depends(protocol_data_root)) -> NodeRecord:
    try:
        node = load_node(root, node_id)
    except InvalidNodeIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found")
    return node


@router.patch("/{node_id}", response_model=NodeRecord)
def patch_node(
    node_id: str,
    payload: NodeUpdate,
    root: Path = Depends(protocol_data_root),
) -> NodeRecord:
    try:
        return update_node(root, node_id, payload)
    except NodeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DuplicatePublicKeyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (InvalidNodeIdentifierError, UnsupportedNodeCategoryError, NodeRegistryError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{node_id}/status", response_model=NodeRecord)
def set_node_status(
    node_id: str,
    payload: NodeStatusChangeRequest,
    root: Path = Depends(protocol_data_root),
) -> NodeRecord:
    try:
        return change_node_status(root, node_id, payload)
    except NodeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidNodeStatusTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except InvalidNodeIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
