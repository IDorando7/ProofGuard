import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from app.schemas.node import (
    NodeCreate,
    NodeRecord,
    NodeStatistics,
    NodeStatus,
    NodeStatusChangeRequest,
    NodeType,
    NodeUpdate,
    normalize_category,
    normalize_categories,
)


NODE_FILENAME = "node.json"
SAFE_NODE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class NodeRegistryError(ValueError):
    """Base error for node registry domain failures."""


class NodeNotFoundError(NodeRegistryError):
    pass


class DuplicatePublicKeyError(NodeRegistryError):
    pass


class InvalidNodeStatusTransitionError(NodeRegistryError):
    pass


class NodeInactiveError(NodeRegistryError):
    pass


class UnsupportedNodeCategoryError(NodeRegistryError):
    pass


class InvalidNodeIdentifierError(NodeRegistryError):
    pass


class NodeStorageError(NodeRegistryError):
    pass


def get_nodes_root(protocol_data_root: Path) -> Path:
    return protocol_data_root / "nodes"


def get_node_dir(protocol_data_root: Path, node_id: str) -> Path:
    _validate_node_id(node_id)
    nodes_root = get_nodes_root(protocol_data_root)
    node_dir = nodes_root / node_id
    try:
        node_dir.resolve().relative_to(nodes_root.resolve())
    except ValueError as exc:
        raise InvalidNodeIdentifierError("Invalid node identifier") from exc
    return node_dir


def create_node(protocol_data_root: Path, payload: NodeCreate) -> NodeRecord:
    categories = normalize_categories(payload.supported_categories)
    _validate_role_categories(payload.node_type, categories)

    if payload.public_key is not None and find_node_by_public_key(protocol_data_root, payload.public_key):
        raise DuplicatePublicKeyError("Public key is already registered")

    now = _utc_now()
    node = NodeRecord(
        node_id=str(uuid.uuid4()),
        node_type=payload.node_type,
        display_name=payload.display_name,
        operator_id=payload.operator_id,
        public_key=payload.public_key,
        supported_categories=categories,
        description=payload.description,
        status=NodeStatus.ACTIVE,
        reputation_score=0.5,
        statistics=NodeStatistics(),
        status_reason="Node registered.",
        created_at=now,
        updated_at=now,
        status_updated_at=now,
    )
    _write_node(protocol_data_root, node)
    return node


def save_node(protocol_data_root: Path, node: NodeRecord) -> NodeRecord:
    saved = NodeRecord.model_validate(
        {
            **node.model_dump(),
            "created_at": node.created_at,
            "updated_at": _utc_now(),
        }
    )
    _write_node(protocol_data_root, saved)
    return saved


def load_node(protocol_data_root: Path, node_id: str) -> NodeRecord | None:
    path = get_node_dir(protocol_data_root, node_id) / NODE_FILENAME
    if not path.exists():
        return None
    if not path.is_file():
        raise NodeStorageError("Stored node record is not a regular file")

    try:
        return NodeRecord.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
        raise NodeStorageError(f"Stored node record is malformed for node '{node_id}'") from exc


def list_nodes(
    protocol_data_root: Path,
    node_type: NodeType | None = None,
    status: NodeStatus | None = None,
    category: str | None = None,
    operator_id: str | None = None,
) -> list[NodeRecord]:
    normalized_category = _normalize_service_category(category) if category is not None else None
    nodes_root = get_nodes_root(protocol_data_root)
    if not nodes_root.exists():
        return []

    nodes: list[NodeRecord] = []
    for path in sorted(nodes_root.glob(f"*/{NODE_FILENAME}")):
        node = load_node(protocol_data_root, path.parent.name)
        if node is None:
            continue
        if node_type is not None and node.node_type != node_type:
            continue
        if status is not None and node.status != status:
            continue
        if normalized_category is not None and normalized_category not in node.supported_categories:
            continue
        if operator_id is not None and node.operator_id != operator_id:
            continue
        nodes.append(node)
    return sorted(nodes, key=lambda node: (node.created_at, node.node_id))


def update_node(
    protocol_data_root: Path,
    node_id: str,
    update: NodeUpdate,
) -> NodeRecord:
    existing = load_node(protocol_data_root, node_id)
    if existing is None:
        raise NodeNotFoundError("Node not found")

    update_data = update.model_dump(exclude_unset=True)
    if update_data.get("display_name", existing.display_name) is None:
        raise NodeRegistryError("Display name cannot be null")
    if "supported_categories" in update_data and update_data["supported_categories"] is None:
        raise NodeRegistryError("Supported categories cannot be null")

    categories = update_data.get("supported_categories", existing.supported_categories)
    categories = normalize_categories(categories)
    _validate_role_categories(existing.node_type, categories)
    update_data["supported_categories"] = categories

    new_public_key = update_data.get("public_key", existing.public_key)
    if new_public_key is not None and new_public_key != existing.public_key:
        matching_node = find_node_by_public_key(protocol_data_root, new_public_key)
        if matching_node is not None and matching_node.node_id != node_id:
            raise DuplicatePublicKeyError("Public key is already registered")

    updated = NodeRecord.model_validate({**existing.model_dump(), **update_data})
    return save_node(protocol_data_root, updated)


def change_node_status(
    protocol_data_root: Path,
    node_id: str,
    request: NodeStatusChangeRequest,
) -> NodeRecord:
    existing = load_node(protocol_data_root, node_id)
    if existing is None:
        raise NodeNotFoundError("Node not found")
    if existing.status == NodeStatus.BANNED and request.status != NodeStatus.BANNED:
        raise InvalidNodeStatusTransitionError("Banned nodes cannot be reactivated or changed")

    now = _utc_now()
    updated = NodeRecord.model_validate(
        {
            **existing.model_dump(),
            "status": request.status,
            "status_reason": request.reason,
            "status_updated_at": now,
        }
    )
    return save_node(protocol_data_root, updated)


def is_node_active(protocol_data_root: Path, node_id: str) -> bool:
    node = load_node(protocol_data_root, node_id)
    return node is not None and node.status == NodeStatus.ACTIVE


def node_supports_category(node: NodeRecord, category: str) -> bool:
    normalized = _normalize_service_category(category)
    return normalized in node.supported_categories


def ensure_node_can_participate(
    protocol_data_root: Path,
    node_id: str,
    category: str | None = None,
) -> NodeRecord:
    node = load_node(protocol_data_root, node_id)
    if node is None:
        raise NodeNotFoundError("Node not found")
    if node.status != NodeStatus.ACTIVE:
        raise NodeInactiveError(f"Node is not active (status: {node.status.value})")
    if category is not None and not node_supports_category(node, category):
        raise UnsupportedNodeCategoryError("Node does not support the requested category")
    return node


def find_node_by_public_key(protocol_data_root: Path, public_key: str) -> NodeRecord | None:
    if not isinstance(public_key, str) or not public_key.strip():
        raise NodeRegistryError("Public key must not be empty")
    normalized_public_key = public_key.strip()
    for node in list_nodes(protocol_data_root):
        if node.public_key == normalized_public_key:
            return node
    return None


def _validate_node_id(node_id: str) -> None:
    if not isinstance(node_id, str) or not SAFE_NODE_ID.fullmatch(node_id):
        raise InvalidNodeIdentifierError("Invalid node identifier")
    if node_id in {".", ".."} or "/" in node_id or "\\" in node_id or Path(node_id).is_absolute():
        raise InvalidNodeIdentifierError("Invalid node identifier")


def _validate_role_categories(node_type: NodeType, categories: list[str]) -> None:
    if node_type in {NodeType.AGENT, NodeType.HYBRID} and not categories:
        raise UnsupportedNodeCategoryError("Agent and hybrid nodes must support at least one category")


def _normalize_service_category(category: str) -> str:
    try:
        return normalize_category(category)
    except ValueError as exc:
        raise UnsupportedNodeCategoryError(str(exc)) from exc


def _write_node(protocol_data_root: Path, node: NodeRecord) -> None:
    node_dir = get_node_dir(protocol_data_root, node.node_id)
    node_dir.mkdir(parents=True, exist_ok=True)
    output_path = node_dir / NODE_FILENAME
    serialized = json.dumps(node.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"

    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=node_dir,
            prefix=".node-",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_name = temporary_file.name
            temporary_file.write(serialized)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        Path(temporary_name).replace(output_path)
    except OSError as exc:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
        raise NodeStorageError("Unable to persist node record") from exc


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
