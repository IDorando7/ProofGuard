from __future__ import annotations

import fcntl
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from app.schemas.audit_run import (
    AUDIT_EVENT_VERSION,
    AUDIT_RUN_VERSION,
    AuditArtifactRef,
    AuditEvent,
    AuditEventLevel,
    AuditEventListResponse,
    AuditEventType,
    AuditExecutionMode,
    AuditRun,
    AuditRunStatus,
    AuditStage,
    initial_stage_states,
    progress_from_stage_states,
    validate_audit_identifier,
)
from app.utils.protocol_serialization import (
    atomic_create_json,
    atomic_write_json,
    protocol_fingerprint,
)


AUDIT_RUNS_DIRECTORY = "audit-runs"
PROJECTS_DIRECTORY = "projects"
AUDIT_RUN_FILENAME = "audit_run.json"
EVENTS_DIRECTORY = "events"
EVENT_INDEX_DIRECTORY = "event-index"
LOCK_FILENAME = ".audit-run.lock"


class AuditRunServiceError(ValueError):
    """Base class for safe integration-layer errors."""


class AuditRunNotFoundError(AuditRunServiceError):
    pass


class AuditRunConflictError(AuditRunServiceError):
    pass


class AuditEventConflictError(AuditRunConflictError):
    pass


class AuditRunStorageError(AuditRunServiceError):
    pass


class InvalidAuditRunIdentifierError(AuditRunServiceError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def get_audit_runs_root(protocol_data_root: Path) -> Path:
    return protocol_data_root / AUDIT_RUNS_DIRECTORY


def get_project_audit_runs_root(protocol_data_root: Path, project_id: str) -> Path:
    _validate_identifier(project_id, "project")
    projects_root = get_audit_runs_root(protocol_data_root) / PROJECTS_DIRECTORY
    project_root = projects_root / project_id
    _ensure_within(project_root, projects_root, "Invalid project identifier")
    return project_root


def get_audit_run_root(
    protocol_data_root: Path,
    project_id: str,
    audit_run_id: str,
) -> Path:
    _validate_identifier(audit_run_id, "AuditRun")
    project_root = get_project_audit_runs_root(protocol_data_root, project_id)
    run_root = project_root / audit_run_id
    _ensure_within(run_root, project_root, "Invalid AuditRun identifier")
    return run_root


def get_audit_run_path(
    protocol_data_root: Path,
    project_id: str,
    audit_run_id: str,
) -> Path:
    return get_audit_run_root(protocol_data_root, project_id, audit_run_id) / AUDIT_RUN_FILENAME


def get_audit_events_root(
    protocol_data_root: Path,
    project_id: str,
    audit_run_id: str,
) -> Path:
    return get_audit_run_root(protocol_data_root, project_id, audit_run_id) / EVENTS_DIRECTORY


def build_audit_run_request_fingerprint(
    project_id: str,
    execution_mode: AuditExecutionMode,
    scope_fingerprint: str,
) -> str:
    _validate_identifier(project_id, "project")
    if len(scope_fingerprint) != 64 or any(
        character not in "0123456789abcdef" for character in scope_fingerprint
    ):
        raise AuditRunServiceError("Scope fingerprint must be lowercase SHA-256")
    return protocol_fingerprint(
        {
            "audit_run_version": AUDIT_RUN_VERSION,
            "project_id": project_id,
            "scope_fingerprint": scope_fingerprint,
            "execution_mode": execution_mode.value,
        }
    )


def create_audit_run(
    protocol_data_root: Path,
    project_id: str,
    execution_mode: AuditExecutionMode,
    scope_fingerprint: str,
    *,
    now: datetime | None = None,
) -> AuditRun:
    _validate_identifier(project_id, "project")
    timestamp = _as_utc(now or utc_now())
    audit_run_id = f"audit_run_{uuid4().hex}"
    stage_states = initial_stage_states()
    run = AuditRun(
        audit_run_id=audit_run_id,
        project_id=project_id,
        status=AuditRunStatus.CREATED,
        current_stage=None,
        execution_mode=execution_mode,
        request_fingerprint=build_audit_run_request_fingerprint(
            project_id,
            execution_mode,
            scope_fingerprint,
        ),
        progress=progress_from_stage_states(stage_states),
        created_at=timestamp,
        updated_at=timestamp,
        stage_states=stage_states,
        event_count=0,
        latest_event_sequence=0,
    )
    run_root = get_audit_run_root(protocol_data_root, project_id, audit_run_id)
    run_root.mkdir(parents=True, exist_ok=False)
    with _audit_run_lock(protocol_data_root, project_id, audit_run_id):
        _write_audit_run(protocol_data_root, run)
        run, _ = _commit_event_locked(
            protocol_data_root,
            run,
            event_type=AuditEventType.AUDIT_CREATED,
            event_level=AuditEventLevel.INFO,
            message="AuditRun created.",
            entity_type="audit_run",
            entity_id=audit_run_id,
            metadata={
                "audit_run_version": AUDIT_RUN_VERSION,
                "execution_mode": execution_mode.value,
                "request_fingerprint": run.request_fingerprint,
            },
            semantic_key="audit-created",
            now=timestamp,
        )
    return run


def load_audit_run(
    protocol_data_root: Path,
    project_id: str,
    audit_run_id: str,
) -> AuditRun | None:
    path = get_audit_run_path(protocol_data_root, project_id, audit_run_id)
    if not path.exists():
        return None
    if not path.is_file():
        raise AuditRunStorageError("Stored AuditRun is not a regular file")
    try:
        run = AuditRun.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
        raise AuditRunStorageError("Stored AuditRun data is malformed") from exc
    if run.project_id != project_id or run.audit_run_id != audit_run_id:
        raise AuditRunStorageError("Stored AuditRun identity does not match its path")
    return run


def require_audit_run(
    protocol_data_root: Path,
    project_id: str,
    audit_run_id: str,
) -> AuditRun:
    run = load_audit_run(protocol_data_root, project_id, audit_run_id)
    if run is None:
        raise AuditRunNotFoundError("AuditRun not found")
    return run


def list_audit_runs(protocol_data_root: Path, project_id: str) -> list[AuditRun]:
    project_root = get_project_audit_runs_root(protocol_data_root, project_id)
    if not project_root.exists():
        return []
    runs: list[AuditRun] = []
    for path in sorted(project_root.glob(f"*/{AUDIT_RUN_FILENAME}")):
        run = load_audit_run(protocol_data_root, project_id, path.parent.name)
        if run is not None:
            runs.append(run)
    return sorted(
        runs,
        key=lambda item: (item.created_at, item.audit_run_id),
        reverse=True,
    )


def list_audit_events(
    protocol_data_root: Path,
    project_id: str,
    audit_run_id: str,
    *,
    after_sequence: int = 0,
    limit: int = 100,
    stage: AuditStage | None = None,
    event_type: AuditEventType | None = None,
    node_id: str | None = None,
) -> AuditEventListResponse:
    if after_sequence < 0:
        raise AuditRunServiceError("after_sequence cannot be negative")
    if limit < 1 or limit > 200:
        raise AuditRunServiceError("limit must be between 1 and 200")
    if node_id is not None:
        _validate_identifier(node_id, "node")
    run = require_audit_run(protocol_data_root, project_id, audit_run_id)
    page: list[AuditEvent] = []
    has_more = False
    for sequence in range(after_sequence + 1, run.latest_event_sequence + 1):
        event = _load_event_at_sequence(
            protocol_data_root,
            project_id,
            audit_run_id,
            sequence,
        )
        if (
            (stage is not None and event.stage != stage)
            or (event_type is not None and event.event_type != event_type)
            or (node_id is not None and event.node_id != node_id)
        ):
            continue
        if len(page) == limit:
            has_more = True
            break
        page.append(event)
    next_after = page[-1].sequence_number if page else after_sequence
    return AuditEventListResponse(
        audit_run_id=audit_run_id,
        after_sequence=after_sequence,
        limit=limit,
        returned=len(page),
        latest_event_sequence=run.latest_event_sequence,
        next_after_sequence=next_after,
        has_more=has_more,
        events=page,
    )


def append_audit_event(
    protocol_data_root: Path,
    project_id: str,
    audit_run_id: str,
    *,
    event_type: AuditEventType,
    message: str,
    stage: AuditStage | None = None,
    event_level: AuditEventLevel = AuditEventLevel.INFO,
    entity_type: str | None = None,
    entity_id: str | None = None,
    node_id: str | None = None,
    operator_id: str | None = None,
    category: str | None = None,
    metadata: dict[str, Any] | None = None,
    artifact_refs: list[AuditArtifactRef] | None = None,
    semantic_key: str | None = None,
    now: datetime | None = None,
) -> tuple[AuditRun, AuditEvent]:
    with _audit_run_lock(protocol_data_root, project_id, audit_run_id):
        run = require_audit_run(protocol_data_root, project_id, audit_run_id)
        if run.status in {AuditRunStatus.COMPLETED, AuditRunStatus.FAILED}:
            raise AuditRunConflictError("Terminal AuditRun cannot accept new events")
        return _commit_event_locked(
            protocol_data_root,
            run,
            event_type=event_type,
            event_level=event_level,
            message=message,
            stage=stage,
            entity_type=entity_type,
            entity_id=entity_id,
            node_id=node_id,
            operator_id=operator_id,
            category=category,
            metadata=metadata or {},
            artifact_refs=artifact_refs or [],
            semantic_key=semantic_key,
            now=now,
        )


Transition = Callable[[AuditRun, int, datetime], AuditRun]


def apply_audit_transition(
    protocol_data_root: Path,
    project_id: str,
    audit_run_id: str,
    *,
    transition: Transition,
    event_type: AuditEventType,
    event_level: AuditEventLevel,
    message: str,
    semantic_key: str,
    stage: AuditStage | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    artifact_refs: list[AuditArtifactRef] | None = None,
    now: datetime | None = None,
) -> tuple[AuditRun, AuditEvent]:
    timestamp = _as_utc(now or utc_now())
    with _audit_run_lock(protocol_data_root, project_id, audit_run_id):
        run = require_audit_run(protocol_data_root, project_id, audit_run_id)
        event_id = _semantic_event_id(audit_run_id, semantic_key)
        existing = _find_event_by_id(
            protocol_data_root,
            project_id,
            audit_run_id,
            event_id,
        )
        if existing is not None:
            _verify_existing_event(
                existing,
                event_type=event_type,
                event_level=event_level,
                message=message,
                stage=stage,
                entity_type=entity_type,
                entity_id=entity_id,
                metadata=metadata or {},
                artifact_refs=artifact_refs or [],
            )
            if run.latest_event_sequence < existing.sequence_number:
                run = transition(
                    run,
                    existing.sequence_number,
                    existing.created_at,
                )
                run = _reconcile_run_cursor(
                    protocol_data_root,
                    run,
                    persist=True,
                )
            return run, existing
        next_sequence = _next_event_sequence(
            protocol_data_root,
            project_id,
            audit_run_id,
            run,
        )
        updated = transition(run, next_sequence, timestamp)
        if updated.audit_run_id != run.audit_run_id or updated.project_id != run.project_id:
            raise AuditRunConflictError("Audit transition cannot change run identity")
        return _commit_event_locked(
            protocol_data_root,
            updated,
            event_type=event_type,
            event_level=event_level,
            message=message,
            stage=stage,
            entity_type=entity_type,
            entity_id=entity_id,
            metadata=metadata or {},
            artifact_refs=artifact_refs or [],
            semantic_key=semantic_key,
            now=timestamp,
            expected_sequence=next_sequence,
        )


def save_audit_run(protocol_data_root: Path, run: AuditRun) -> AuditRun:
    """Persist an already validated run from trusted integration code."""
    with _audit_run_lock(protocol_data_root, run.project_id, run.audit_run_id):
        existing = require_audit_run(
            protocol_data_root,
            run.project_id,
            run.audit_run_id,
        )
        if existing.status == AuditRunStatus.COMPLETED and run != existing:
            raise AuditRunConflictError("Completed AuditRun is immutable")
        _write_audit_run(protocol_data_root, run)
    return run


def set_audit_run_routing_id(
    protocol_data_root: Path,
    project_id: str,
    audit_run_id: str,
    routing_id: str,
) -> AuditRun:
    """Trusted integration update for the routing entity reference."""
    _validate_identifier(routing_id, "routing")
    with _audit_run_lock(protocol_data_root, project_id, audit_run_id):
        run = require_audit_run(protocol_data_root, project_id, audit_run_id)
        if run.status in {AuditRunStatus.COMPLETED, AuditRunStatus.FAILED}:
            raise AuditRunConflictError("Terminal AuditRun references are immutable")
        if run.routing_id is not None and run.routing_id != routing_id:
            raise AuditRunConflictError("AuditRun already references another routing")
        if run.routing_id == routing_id:
            return run
        updated = AuditRun.model_validate(
            {
                **run.model_dump(),
                "routing_id": routing_id,
                "updated_at": utc_now(),
            }
        )
        _write_audit_run(protocol_data_root, updated)
        return updated


def _commit_event_locked(
    protocol_data_root: Path,
    run: AuditRun,
    *,
    event_type: AuditEventType,
    event_level: AuditEventLevel,
    message: str,
    stage: AuditStage | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    node_id: str | None = None,
    operator_id: str | None = None,
    category: str | None = None,
    metadata: dict[str, Any],
    artifact_refs: list[AuditArtifactRef] | None = None,
    semantic_key: str | None,
    now: datetime | None,
    expected_sequence: int | None = None,
) -> tuple[AuditRun, AuditEvent]:
    timestamp = _as_utc(now or utc_now())
    event_id = (
        _semantic_event_id(run.audit_run_id, semantic_key)
        if semantic_key is not None
        else f"audit_event_{uuid4().hex}"
    )
    existing = (
        _find_event_by_id(
            protocol_data_root,
            run.project_id,
            run.audit_run_id,
            event_id,
        )
        if semantic_key is not None
        else None
    )
    if existing is not None:
        _verify_existing_event(
            existing,
            event_type=event_type,
            event_level=event_level,
            message=message,
            stage=stage,
            entity_type=entity_type,
            entity_id=entity_id,
            node_id=node_id,
            operator_id=operator_id,
            category=category,
            metadata=metadata,
            artifact_refs=artifact_refs or [],
        )
        run = _reconcile_run_cursor(protocol_data_root, run, persist=True)
        return run, existing
    sequence = expected_sequence or _next_event_sequence(
        protocol_data_root,
        run.project_id,
        run.audit_run_id,
        run,
    )
    event = AuditEvent(
        audit_event_id=event_id,
        audit_run_id=run.audit_run_id,
        sequence_number=sequence,
        event_type=event_type,
        stage=stage,
        event_level=event_level,
        message=message,
        entity_type=entity_type,
        entity_id=entity_id,
        node_id=node_id,
        operator_id=operator_id,
        category=category,
        metadata=metadata,
        artifact_refs=artifact_refs or [],
        created_at=timestamp,
    )
    updated = AuditRun.model_validate(
        {
            **run.model_dump(),
            "event_count": sequence,
            "latest_event_sequence": sequence,
            "updated_at": max(run.updated_at, timestamp),
        }
    )
    event_path = _event_path(
        protocol_data_root,
        run.project_id,
        run.audit_run_id,
        sequence,
    )
    if not atomic_create_json(event_path, event, temporary_prefix=".audit-event-"):
        raise AuditRunStorageError("Audit event sequence is already occupied")
    try:
        index_path = _event_index_path(
            protocol_data_root,
            run.project_id,
            run.audit_run_id,
            event_id,
        )
        index_payload = {
            "audit_event_id": event_id,
            "sequence_number": sequence,
        }
        if not atomic_create_json(
            index_path,
            index_payload,
            temporary_prefix=".audit-event-index-",
        ):
            existing_index = _load_event_index(index_path)
            if existing_index != index_payload:
                raise AuditEventConflictError(
                    "AuditEvent index has conflicting content"
                )
        _write_audit_run(protocol_data_root, updated)
    except Exception:
        # The append remains durable and can be reconciled from its sequence on retry.
        raise
    return updated, event


def _write_audit_run(protocol_data_root: Path, run: AuditRun) -> None:
    path = get_audit_run_path(
        protocol_data_root,
        run.project_id,
        run.audit_run_id,
    )
    try:
        atomic_write_json(path, run, temporary_prefix=".audit-run-")
    except OSError as exc:
        raise AuditRunStorageError("Unable to persist AuditRun") from exc


def _event_path(
    protocol_data_root: Path,
    project_id: str,
    audit_run_id: str,
    sequence: int,
) -> Path:
    if sequence < 1:
        raise AuditRunServiceError("Audit event sequence must be positive")
    return get_audit_events_root(
        protocol_data_root,
        project_id,
        audit_run_id,
    ) / f"{sequence:08d}.json"


def _event_index_path(
    protocol_data_root: Path,
    project_id: str,
    audit_run_id: str,
    event_id: str,
) -> Path:
    _validate_identifier(event_id, "AuditEvent")
    run_root = get_audit_run_root(protocol_data_root, project_id, audit_run_id)
    index_root = run_root / EVENT_INDEX_DIRECTORY
    path = index_root / f"{event_id}.json"
    _ensure_within(path, index_root, "Invalid AuditEvent identifier")
    return path


def _load_event_at_sequence(
    protocol_data_root: Path,
    project_id: str,
    audit_run_id: str,
    sequence: int,
) -> AuditEvent:
    path = _event_path(protocol_data_root, project_id, audit_run_id, sequence)
    if not path.is_file():
        raise AuditRunStorageError("Stored AuditEvent sequence has a gap")
    try:
        event = AuditEvent.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
        raise AuditRunStorageError("Stored AuditEvent data is malformed") from exc
    if (
        event.audit_run_id != audit_run_id
        or event.sequence_number != sequence
        or path.name != f"{sequence:08d}.json"
    ):
        raise AuditRunStorageError("Stored AuditEvent identity does not match its path")
    return event


def _load_all_events(
    protocol_data_root: Path,
    project_id: str,
    audit_run_id: str,
) -> list[AuditEvent]:
    root = get_audit_events_root(protocol_data_root, project_id, audit_run_id)
    if not root.exists():
        return []
    events: list[AuditEvent] = []
    for path in sorted(root.glob("*.json")):
        try:
            event = AuditEvent.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
            raise AuditRunStorageError("Stored AuditEvent data is malformed") from exc
        if (
            event.audit_run_id != audit_run_id
            or path.name != f"{event.sequence_number:08d}.json"
        ):
            raise AuditRunStorageError("Stored AuditEvent identity does not match its path")
        events.append(event)
    expected = list(range(1, len(events) + 1))
    if [event.sequence_number for event in events] != expected:
        raise AuditRunStorageError("Stored AuditEvent sequence is not contiguous")
    return events


def _find_event_by_id(
    protocol_data_root: Path,
    project_id: str,
    audit_run_id: str,
    event_id: str,
) -> AuditEvent | None:
    index_path = _event_index_path(
        protocol_data_root,
        project_id,
        audit_run_id,
        event_id,
    )
    if index_path.is_file():
        index = _load_event_index(index_path)
        if index["audit_event_id"] != event_id:
            raise AuditRunStorageError("AuditEvent index identity is malformed")
        event = _load_event_at_sequence(
            protocol_data_root,
            project_id,
            audit_run_id,
            index["sequence_number"],
        )
        if event.audit_event_id != event_id:
            raise AuditRunStorageError("AuditEvent index points to another event")
        return event
    # Recovery path for an event written immediately before its index/run cursor.
    run = load_audit_run(protocol_data_root, project_id, audit_run_id)
    sequence = 1 if run is None else run.latest_event_sequence + 1
    tail_events: list[AuditEvent] = []
    while _event_path(
        protocol_data_root,
        project_id,
        audit_run_id,
        sequence,
    ).is_file():
        tail_events.append(
            _load_event_at_sequence(
                protocol_data_root,
                project_id,
                audit_run_id,
                sequence,
            )
        )
        sequence += 1
    for event in tail_events:
        if event.audit_event_id == event_id:
            return event
    return None


def _load_event_index(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AuditRunStorageError("Stored AuditEvent index is malformed") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"audit_event_id", "sequence_number"}
        or not isinstance(payload["audit_event_id"], str)
        or not isinstance(payload["sequence_number"], int)
        or payload["sequence_number"] < 1
    ):
        raise AuditRunStorageError("Stored AuditEvent index is malformed")
    return payload


def _next_event_sequence(
    protocol_data_root: Path,
    project_id: str,
    audit_run_id: str,
    run: AuditRun,
) -> int:
    if run.latest_event_sequence > 0:
        _load_event_at_sequence(
            protocol_data_root,
            project_id,
            audit_run_id,
            run.latest_event_sequence,
        )
    sequence = run.latest_event_sequence + 1
    # Reconcile the narrow crash window where an event reached disk before the
    # compact AuditRun cursor was replaced. Normal appends remain O(1).
    while _event_path(
        protocol_data_root,
        project_id,
        audit_run_id,
        sequence,
    ).is_file():
        _load_event_at_sequence(
            protocol_data_root,
            project_id,
            audit_run_id,
            sequence,
        )
        sequence += 1
    return sequence


def _reconcile_run_cursor(
    protocol_data_root: Path,
    run: AuditRun,
    *,
    persist: bool,
) -> AuditRun:
    latest = run.latest_event_sequence
    latest_at = run.updated_at
    sequence = latest + 1
    while _event_path(
        protocol_data_root,
        run.project_id,
        run.audit_run_id,
        sequence,
    ).is_file():
        event = _load_event_at_sequence(
            protocol_data_root,
            run.project_id,
            run.audit_run_id,
            sequence,
        )
        latest = sequence
        latest_at = max(latest_at, event.created_at)
        sequence += 1
    if latest == run.latest_event_sequence:
        return run
    reconciled = AuditRun.model_validate(
        {
            **run.model_dump(),
            "event_count": latest,
            "latest_event_sequence": latest,
            "updated_at": latest_at,
        }
    )
    if persist:
        _write_audit_run(protocol_data_root, reconciled)
    return reconciled


def _semantic_event_id(audit_run_id: str, semantic_key: str) -> str:
    if not semantic_key or len(semantic_key) > 512:
        raise AuditRunServiceError("Audit event semantic key is invalid")
    digest = protocol_fingerprint(
        {
            "event_version": AUDIT_EVENT_VERSION,
            "audit_run_id": audit_run_id,
            "semantic_key": semantic_key,
        }
    )
    return f"audit_event_{digest}"


def _verify_existing_event(
    event: AuditEvent,
    *,
    event_type: AuditEventType,
    event_level: AuditEventLevel,
    message: str,
    stage: AuditStage | None,
    entity_type: str | None,
    entity_id: str | None,
    node_id: str | None = None,
    operator_id: str | None = None,
    category: str | None = None,
    metadata: dict[str, Any],
    artifact_refs: list[AuditArtifactRef],
) -> None:
    expected = {
        "event_type": event_type,
        "event_level": event_level,
        "message": message,
        "stage": stage,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "node_id": node_id,
        "operator_id": operator_id,
        "category": category,
        "metadata": metadata,
        "artifact_refs": artifact_refs,
    }
    actual = {key: getattr(event, key) for key in expected}
    if actual != expected:
        raise AuditEventConflictError(
            "Existing semantic AuditEvent has conflicting structured content"
        )


@contextmanager
def _audit_run_lock(
    protocol_data_root: Path,
    project_id: str,
    audit_run_id: str,
) -> Iterator[None]:
    run_root = get_audit_run_root(protocol_data_root, project_id, audit_run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    lock_path = run_root / LOCK_FILENAME
    try:
        lock_file = lock_path.open("a", encoding="utf-8")
    except OSError as exc:
        raise AuditRunStorageError("Unable to open AuditRun lock") from exc
    with lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            raise AuditRunStorageError("Unable to acquire AuditRun lock") from exc
        try:
            yield
        finally:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except OSError as exc:
                raise AuditRunStorageError("Unable to release AuditRun lock") from exc


def _validate_identifier(value: str, label: str) -> None:
    try:
        validate_audit_identifier(value, label)
    except ValueError as exc:
        raise InvalidAuditRunIdentifierError(str(exc)) from exc


def _ensure_within(path: Path, root: Path, message: str) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise InvalidAuditRunIdentifierError(message) from exc


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AuditRunServiceError("AuditRun timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)
