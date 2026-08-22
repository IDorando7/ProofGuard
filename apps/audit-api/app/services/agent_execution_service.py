from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import ValidationError

from app.schemas.agent_execution import AgentExecutionRecord, AgentExecutionStatus
from app.schemas.audit_run import validate_audit_identifier
from app.services.audit_run_service import (
    get_audit_run_root,
    get_audit_runs_root,
    require_audit_run,
)
from app.utils.protocol_serialization import atomic_create_json, atomic_write_json


EXECUTIONS_DIRECTORY = "agent-executions"
EXECUTION_FILENAME = "execution.json"
ENTITY_INDEX_DIRECTORY = "entity-index"


class AgentExecutionServiceError(ValueError):
    pass


class AgentExecutionNotFoundError(AgentExecutionServiceError):
    pass


class AgentExecutionConflictError(AgentExecutionServiceError):
    pass


class AgentExecutionStorageError(AgentExecutionServiceError):
    pass


def logical_agent_execution_id(audit_run_id: str, assignment_id: str) -> str:
    digest = hashlib.sha256(
        f"agent_execution_v1\0{audit_run_id}\0{assignment_id}".encode("utf-8")
    ).hexdigest()
    return f"agent_execution_{digest[:40]}"


def get_agent_executions_root(
    protocol_data_root: Path, project_id: str, audit_run_id: str
) -> Path:
    return get_audit_run_root(protocol_data_root, project_id, audit_run_id) / EXECUTIONS_DIRECTORY


def get_agent_execution_path(
    protocol_data_root: Path,
    project_id: str,
    audit_run_id: str,
    agent_execution_id: str,
) -> Path:
    if not agent_execution_id.startswith("agent_execution_"):
        raise AgentExecutionServiceError("Invalid AgentExecution identifier")
    root = get_agent_executions_root(protocol_data_root, project_id, audit_run_id)
    path = root / agent_execution_id / EXECUTION_FILENAME
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise AgentExecutionServiceError("Invalid AgentExecution identifier") from exc
    return path


def create_agent_execution(
    protocol_data_root: Path,
    record: AgentExecutionRecord,
) -> tuple[AgentExecutionRecord, bool]:
    require_audit_run(
        protocol_data_root, record.project_id, record.audit_run_id
    )
    expected_id = logical_agent_execution_id(
        record.audit_run_id, record.routing_assignment_id
    )
    if record.agent_execution_id != expected_id:
        raise AgentExecutionConflictError(
            "AgentExecution identity does not match its assignment"
        )
    path = get_agent_execution_path(
        protocol_data_root,
        record.project_id,
        record.audit_run_id,
        record.agent_execution_id,
    )
    created = atomic_create_json(path, record, temporary_prefix=".agent-execution-")
    if created:
        return record, True
    existing = load_agent_execution(
        protocol_data_root,
        record.project_id,
        record.audit_run_id,
        record.agent_execution_id,
    )
    if existing is None:
        raise AgentExecutionStorageError("AgentExecution create was not durable")
    immutable_fields = (
        "audit_run_id",
        "project_id",
        "routing_id",
        "routing_assignment_id",
        "node_id",
        "operator_id",
        "category",
        "executor_type",
        "agent_type",
        "agent_version",
        "source_fingerprint",
    )
    if any(getattr(existing, name) != getattr(record, name) for name in immutable_fields):
        raise AgentExecutionConflictError(
            "Existing AgentExecution has conflicting immutable inputs"
        )
    return existing, False


def save_agent_execution(
    protocol_data_root: Path,
    record: AgentExecutionRecord,
) -> AgentExecutionRecord:
    existing = require_agent_execution(
        protocol_data_root,
        record.project_id,
        record.audit_run_id,
        record.agent_execution_id,
    )
    if existing.status == AgentExecutionStatus.COMPLETED and record != existing:
        raise AgentExecutionConflictError("Completed AgentExecution is immutable")
    immutable_fields = (
        "audit_run_id",
        "project_id",
        "routing_id",
        "routing_assignment_id",
        "node_id",
        "operator_id",
        "category",
        "executor_type",
        "agent_type",
        "agent_version",
        "source_fingerprint",
        "created_at",
    )
    if any(getattr(existing, name) != getattr(record, name) for name in immutable_fields):
        raise AgentExecutionConflictError("AgentExecution immutable inputs changed")
    path = get_agent_execution_path(
        protocol_data_root,
        record.project_id,
        record.audit_run_id,
        record.agent_execution_id,
    )
    atomic_write_json(path, record, temporary_prefix=".agent-execution-")
    return record


def load_agent_execution(
    protocol_data_root: Path,
    project_id: str,
    audit_run_id: str,
    agent_execution_id: str,
) -> AgentExecutionRecord | None:
    path = get_agent_execution_path(
        protocol_data_root, project_id, audit_run_id, agent_execution_id
    )
    if not path.exists():
        return None
    if not path.is_file():
        raise AgentExecutionStorageError("Stored AgentExecution is not a file")
    try:
        record = AgentExecutionRecord.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
        raise AgentExecutionStorageError("Stored AgentExecution is malformed") from exc
    if (
        record.project_id != project_id
        or record.audit_run_id != audit_run_id
        or record.agent_execution_id != agent_execution_id
    ):
        raise AgentExecutionStorageError(
            "Stored AgentExecution identity does not match its path"
        )
    return record


def require_agent_execution(
    protocol_data_root: Path,
    project_id: str,
    audit_run_id: str,
    agent_execution_id: str,
) -> AgentExecutionRecord:
    record = load_agent_execution(
        protocol_data_root, project_id, audit_run_id, agent_execution_id
    )
    if record is None:
        raise AgentExecutionNotFoundError("AgentExecution not found")
    return record


def list_agent_executions(
    protocol_data_root: Path,
    project_id: str,
    audit_run_id: str,
) -> list[AgentExecutionRecord]:
    require_audit_run(protocol_data_root, project_id, audit_run_id)
    root = get_agent_executions_root(protocol_data_root, project_id, audit_run_id)
    if not root.exists():
        return []
    records: list[AgentExecutionRecord] = []
    for path in sorted(root.glob(f"*/{EXECUTION_FILENAME}")):
        record = load_agent_execution(
            protocol_data_root, project_id, audit_run_id, path.parent.name
        )
        if record is not None:
            records.append(record)
    return sorted(
        records,
        key=lambda item: (
            item.category.value,
            item.node_id,
            item.routing_assignment_id,
            item.agent_execution_id,
        ),
    )


def index_agent_execution_entities(
    protocol_data_root: Path,
    record: AgentExecutionRecord,
) -> None:
    """Create append-only reverse links for future Finding/Submission views."""
    if record.status != AgentExecutionStatus.COMPLETED:
        raise AgentExecutionConflictError(
            "Only completed executions can publish entity attribution"
        )
    for entity_type, entity_ids in (
        ("findings", record.finding_ids),
        ("submissions", record.submission_ids),
    ):
        for entity_id in entity_ids:
            path = _entity_attribution_path(
                protocol_data_root,
                entity_type,
                entity_id,
                record.agent_execution_id,
            )
            payload = {
                "entity_type": entity_type[:-1],
                "entity_id": entity_id,
                "audit_run_id": record.audit_run_id,
                "project_id": record.project_id,
                "routing_id": record.routing_id,
                "routing_assignment_id": record.routing_assignment_id,
                "agent_execution_id": record.agent_execution_id,
                "node_id": record.node_id,
                "operator_id": record.operator_id,
                "category": record.category.value,
            }
            created = atomic_create_json(
                path, payload, temporary_prefix=".agent-attribution-"
            )
            if not created:
                try:
                    existing = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    raise AgentExecutionStorageError(
                        "Stored execution attribution is malformed"
                    ) from exc
                if existing != payload:
                    raise AgentExecutionConflictError(
                        "Execution attribution conflicts with existing data"
                    )


def list_agent_execution_attributions(
    protocol_data_root: Path,
    entity_type: str,
    entity_id: str,
) -> list[dict[str, str]]:
    plural = {"finding": "findings", "submission": "submissions"}.get(entity_type)
    if plural is None:
        raise AgentExecutionServiceError("Unsupported attribution entity type")
    validate_audit_identifier(entity_id, "entity")
    root = (
        get_audit_runs_root(protocol_data_root)
        / ENTITY_INDEX_DIRECTORY
        / plural
        / entity_id
    )
    if not root.exists():
        return []
    records: list[dict[str, str]] = []
    for path in sorted(root.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AgentExecutionStorageError(
                "Stored execution attribution is malformed"
            ) from exc
        if not isinstance(payload, dict) or payload.get("entity_id") != entity_id:
            raise AgentExecutionStorageError(
                "Stored execution attribution identity is inconsistent"
            )
        records.append(payload)
    return sorted(
        records,
        key=lambda item: (
            item["audit_run_id"],
            item["agent_execution_id"],
        ),
    )


def _entity_attribution_path(
    protocol_data_root: Path,
    entity_type: str,
    entity_id: str,
    agent_execution_id: str,
) -> Path:
    if entity_type not in {"findings", "submissions"}:
        raise AgentExecutionServiceError("Unsupported attribution entity type")
    validate_audit_identifier(entity_id, "entity")
    validate_audit_identifier(agent_execution_id, "AgentExecution")
    index_root = get_audit_runs_root(protocol_data_root) / ENTITY_INDEX_DIRECTORY
    entity_root = index_root / entity_type / entity_id
    path = entity_root / f"{agent_execution_id}.json"
    try:
        path.resolve().relative_to(index_root.resolve())
    except ValueError as exc:
        raise AgentExecutionServiceError("Invalid attribution identifier") from exc
    return path
