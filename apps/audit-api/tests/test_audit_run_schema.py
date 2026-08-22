from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas.audit_run import (
    EXECUTABLE_AUDIT_STAGES,
    AuditArtifactRef,
    AuditArtifactType,
    AuditEvent,
    AuditEventType,
    AuditExecutionMode,
    AuditProgress,
    AuditRun,
    AuditRunCreateRequest,
    AuditRunStatus,
    AuditStage,
    AuditStageState,
    AuditStageStatus,
    StageResult,
    initial_stage_states,
    progress_from_stage_states,
)


NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def _artifact(**updates) -> AuditArtifactRef:
    values = {
        "artifact_id": "artifact_stdout_1",
        "audit_run_id": "audit_run_1",
        "artifact_type": AuditArtifactType.FOUNDRY_STDOUT,
        "entity_type": "reproduction",
        "entity_id": "reproduction_1",
        "display_name": "forge-stdout.txt",
        "content_type": "text/plain",
        "storage_ref": "audit-artifacts/audit_run_1/artifact_stdout_1",
        "size_bytes": 42,
        "sha256": "a" * 64,
        "created_at": NOW,
    }
    values.update(updates)
    return AuditArtifactRef(**values)


def _run(**updates) -> AuditRun:
    states = updates.pop("stage_states", initial_stage_states())
    values = {
        "audit_run_id": "audit_run_1",
        "project_id": "project_1",
        "status": AuditRunStatus.CREATED,
        "execution_mode": AuditExecutionMode.LOCAL_SIMULATOR,
        "request_fingerprint": "b" * 64,
        "progress": progress_from_stage_states(states),
        "created_at": NOW,
        "updated_at": NOW,
        "stage_states": states,
    }
    values.update(updates)
    return AuditRun(**values)


def _completed_states() -> list[AuditStageState]:
    states = []
    for stage in AuditStage:
        states.append(
            AuditStageState(
                stage=stage,
                status=AuditStageStatus.COMPLETED,
                attempt_count=1,
                started_at=NOW,
                completed_at=NOW + timedelta(seconds=1),
                progress_current=1,
                progress_total=1,
                event_sequence_start=1,
                event_sequence_end=2,
            )
        )
    return states


def test_valid_created_running_completed_and_failed_audit_runs():
    created = _run()
    assert created.status == AuditRunStatus.CREATED

    running_states = initial_stage_states()
    running_states[0] = AuditStageState(
        stage=AuditStage.PREPARING,
        status=AuditStageStatus.RUNNING,
        attempt_count=1,
        started_at=NOW,
        event_sequence_start=2,
    )
    running = _run(
        status=AuditRunStatus.RUNNING,
        current_stage=AuditStage.PREPARING,
        started_at=NOW,
        stage_states=running_states,
        progress=progress_from_stage_states(running_states),
    )
    assert running.current_stage == AuditStage.PREPARING

    completed_states = _completed_states()
    completed = _run(
        status=AuditRunStatus.COMPLETED,
        current_stage=AuditStage.COMPLETED,
        started_at=NOW,
        completed_at=NOW + timedelta(seconds=2),
        updated_at=NOW + timedelta(seconds=2),
        stage_states=completed_states,
        progress=progress_from_stage_states(completed_states),
    )
    assert completed.progress.progress_percentage == 100

    failed_states = initial_stage_states()
    failed_states[0] = AuditStageState(
        stage=AuditStage.PREPARING,
        status=AuditStageStatus.FAILED,
        attempt_count=1,
        started_at=NOW,
        completed_at=NOW + timedelta(seconds=1),
        error_code="PREPARE_FAILED",
        error_message="Scope preparation failed safely.",
        event_sequence_start=2,
        event_sequence_end=3,
    )
    failed = _run(
        status=AuditRunStatus.FAILED,
        current_stage=AuditStage.PREPARING,
        started_at=NOW,
        failed_at=NOW + timedelta(seconds=1),
        updated_at=NOW + timedelta(seconds=1),
        failure_stage=AuditStage.PREPARING,
        error_code="PREPARE_FAILED",
        error_message="Scope preparation failed safely.",
        stage_states=failed_states,
        progress=progress_from_stage_states(failed_states),
    )
    assert failed.failure_stage == AuditStage.PREPARING


@pytest.mark.parametrize(
    "updates",
    [
        {"project_id": "../escape"},
        {"execution_mode": "invalid"},
        {"status": "COMPLETED", "current_stage": "COMPLETED"},
        {"completed_at": NOW},
        {
            "status": "FAILED",
            "current_stage": "PREPARING",
            "started_at": NOW,
            "failed_at": NOW,
            "failure_stage": "PREPARING",
        },
        {"status": "RUNNING", "current_stage": None, "started_at": NOW},
    ],
)
def test_audit_run_rejects_invalid_identity_mode_and_lifecycle(updates):
    with pytest.raises(ValidationError):
        _run(**updates)


def test_all_required_audit_stage_values_exist():
    assert [stage.value for stage in AuditStage] == [
        "PREPARING",
        "ROUTING",
        "EXECUTING_AGENTS",
        "REPRODUCING",
        "VALIDATING",
        "CALIBRATING",
        "CLUSTERING",
        "ASSESSING_QUALITY",
        "REWARDING",
        "REPORTING",
        "COMPLETED",
    ]
    assert len(EXECUTABLE_AUDIT_STAGES) == 10


def test_pending_running_completed_and_failed_stage_states_are_valid():
    assert AuditStageState(stage="PREPARING").status == AuditStageStatus.PENDING
    running = AuditStageState(
        stage="PREPARING",
        status="RUNNING",
        attempt_count=1,
        started_at=NOW,
        progress_current=3,
        progress_total=5,
    )
    assert running.progress_current == 3
    completed = running.model_dump()
    completed.update(status="COMPLETED", completed_at=NOW, progress_current=5)
    assert AuditStageState(**completed).status == AuditStageStatus.COMPLETED
    failed = running.model_dump()
    failed.update(
        status="FAILED",
        completed_at=NOW,
        error_code="HANDLER_ERROR",
        error_message="Handler failed safely.",
    )
    assert AuditStageState(**failed).error_code == "HANDLER_ERROR"


@pytest.mark.parametrize(
    "values",
    [
        {"progress_current": -1},
        {"progress_current": 2, "progress_total": 1},
        {"status": "COMPLETED", "attempt_count": 1, "started_at": NOW},
        {
            "status": "FAILED",
            "attempt_count": 1,
            "started_at": NOW,
            "completed_at": NOW,
        },
    ],
)
def test_stage_state_rejects_invalid_progress_or_terminal_metadata(values):
    with pytest.raises(ValidationError):
        AuditStageState(stage="PREPARING", **values)


def test_progress_is_derived_from_stage_states_not_enum_comparison():
    states = initial_stage_states()
    for index in range(3):
        states[index] = AuditStageState(
            stage=states[index].stage,
            status="COMPLETED",
            attempt_count=1,
            started_at=NOW,
            completed_at=NOW,
        )
    progress = progress_from_stage_states(states)
    assert progress == AuditProgress(
        completed_stage_count=3,
        total_stage_count=10,
        progress_percentage=30,
    )
    with pytest.raises(ValidationError):
        AuditProgress(
            completed_stage_count=3,
            total_stage_count=10,
            progress_percentage=31,
        )


def test_event_supports_future_node_and_reward_structured_data():
    event = AuditEvent(
        audit_event_id="audit_event_1",
        audit_run_id="audit_run_1",
        sequence_number=9,
        event_type=AuditEventType.OPERATOR_REWARD_CALCULATED,
        stage=AuditStage.REWARDING,
        message="Operator reward calculation persisted.",
        entity_type="reward_allocation",
        entity_id="allocation_1",
        node_id="node_17",
        operator_id="operator_4",
        category="reentrancy",
        metadata={
            "severity_weight": str(Decimal("16.000000")),
            "uniqueness": str(Decimal("0.782900")),
            "finding_score": str(Decimal("12.526400")),
            "quality_score": str(Decimal("0.930000")),
            "quality_weight": str(Decimal("0.864900")),
            "reward": str(Decimal("320.000000")),
            "before": "0.580000",
            "after": "0.660000",
        },
        created_at=NOW,
    )
    payload = event.model_dump(mode="json")
    assert payload["node_id"] == "node_17"
    assert payload["metadata"]["quality_weight"] == "0.864900"
    assert "320" not in event.message


@pytest.mark.parametrize(
    "metadata",
    [
        {"decimal": Decimal("1.2")},
        {"binary": b"secret"},
        {"number": float("nan")},
        {"host_path": "/tmp/private.log"},
        {"path": "/home/user/private.log"},
        {"large": "x" * 4097},
    ],
)
def test_event_metadata_is_json_safe_bounded_and_path_free(metadata):
    with pytest.raises(ValidationError):
        AuditEvent(
            audit_event_id="audit_event_1",
            audit_run_id="audit_run_1",
            sequence_number=1,
            event_type="STAGE_PROGRESS",
            message="Structured event.",
            metadata=metadata,
            created_at=NOW,
        )


def test_foundry_stdout_artifact_is_logical_and_attachable_to_event():
    artifact = _artifact()
    event = AuditEvent(
        audit_event_id="audit_event_2",
        audit_run_id="audit_run_1",
        sequence_number=2,
        event_type="REPRODUCTION_LOG",
        stage="REPRODUCING",
        message="Foundry stdout artifact recorded.",
        entity_type="reproduction",
        entity_id="reproduction_1",
        artifact_refs=[artifact],
        metadata={"stream": "stdout", "test_name": "test_ReentrantWithdraw"},
        created_at=NOW,
    )
    serialized = event.model_dump_json()
    assert "audit-artifacts/audit_run_1/artifact_stdout_1" in serialized
    assert "/tmp/" not in serialized
    assert "/home/" not in serialized


@pytest.mark.parametrize(
    "updates",
    [
        {"storage_ref": "/tmp/forge.stdout"},
        {"storage_ref": "../forge.stdout"},
        {"storage_ref": "audit-artifacts/../forge.stdout"},
        {"storage_ref": "file:///tmp/forge.stdout"},
        {"sha256": "ABC"},
        {"display_name": "/tmp/forge.stdout"},
        {"entity_id": None},
        {"audit_run_id": "../escape"},
    ],
)
def test_artifact_reference_rejects_host_paths_and_invalid_identity(updates):
    with pytest.raises(ValidationError):
        _artifact(**updates)


def test_public_create_request_cannot_forge_state_or_downstream_ids():
    assert AuditRunCreateRequest().execution_mode == AuditExecutionMode.LOCAL_SIMULATOR
    for payload in (
        {"execution_mode": "remote_network"},
        {"status": "COMPLETED"},
        {"current_stage": "REWARDING"},
        {"reward_cycle_id": "reward_1"},
        {"routing_id": "routing_1"},
    ):
        with pytest.raises(ValidationError):
            AuditRunCreateRequest(**payload)


def test_stage_result_contains_only_serializable_refs_and_metadata():
    result = StageResult(
        stage="REPRODUCING",
        success=True,
        summary="One reproduction completed.",
        artifact_refs=[_artifact()],
        entity_refs=[{"entity_type": "reproduction", "entity_id": "reproduction_1"}],
        metadata={"exit_code": 0, "status": "reproduced"},
    )
    assert result.entity_refs[0].entity_id == "reproduction_1"
    with pytest.raises(ValidationError):
        StageResult(
            stage="REPRODUCING",
            success=True,
            summary="Invalid result.",
            metadata={"object": Path("opaque")},
        )

