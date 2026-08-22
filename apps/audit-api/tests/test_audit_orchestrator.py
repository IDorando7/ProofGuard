from datetime import datetime, timezone

import pytest

from app.services import audit_run_service
from app.schemas.audit_run import (
    EXECUTABLE_AUDIT_STAGES,
    AuditArtifactRef,
    AuditEventType,
    AuditExecutionMode,
    AuditRunStatus,
    AuditStage,
    AuditStageStatus,
    StageResult,
)
from app.services.audit_orchestrator import (
    AuditOrchestrator,
    InvalidAuditTransitionError,
)
from app.services.audit_run_service import (
    AuditEventConflictError,
    append_audit_event,
    build_audit_run_request_fingerprint,
    create_audit_run,
    list_audit_events,
    load_audit_run,
)


def _created(tmp_path, project_id="project_1"):
    return create_audit_run(
        tmp_path,
        project_id,
        AuditExecutionMode.LOCAL_SIMULATOR,
        "a" * 64,
    )


def _complete_current(orchestrator, project_id, run):
    return orchestrator.complete_stage(
        project_id,
        run.audit_run_id,
        StageResult(
            stage=run.current_stage,
            success=True,
            summary=f"{run.current_stage.value} completed.",
            progress_current=1,
            progress_total=1,
        ),
    )


def test_created_to_preparing_and_linear_stage_sequence_persist(tmp_path):
    created = _created(tmp_path)
    orchestrator = AuditOrchestrator(tmp_path)
    run = orchestrator.start("project_1", created.audit_run_id)
    assert run.status == AuditRunStatus.RUNNING
    assert run.current_stage == AuditStage.PREPARING

    for index, stage in enumerate(EXECUTABLE_AUDIT_STAGES):
        assert run.current_stage == stage
        run = _complete_current(orchestrator, "project_1", run)
        assert run.progress.completed_stage_count == index + 1
        if index + 1 < len(EXECUTABLE_AUDIT_STAGES):
            run = orchestrator.start_stage(
                "project_1",
                run.audit_run_id,
                EXECUTABLE_AUDIT_STAGES[index + 1],
            )

    run = orchestrator.complete_audit("project_1", run.audit_run_id)
    assert run.status == AuditRunStatus.COMPLETED
    assert run.current_stage == AuditStage.COMPLETED
    assert run.completed_at is not None
    assert run.progress.progress_percentage == 100
    assert load_audit_run(tmp_path, "project_1", run.audit_run_id) == run


def test_illegal_forward_backward_and_restart_transitions_fail(tmp_path):
    created = _created(tmp_path)
    orchestrator = AuditOrchestrator(tmp_path)
    with pytest.raises(InvalidAuditTransitionError):
        orchestrator.start_stage("project_1", created.audit_run_id, AuditStage.REWARDING)
    run = orchestrator.start("project_1", created.audit_run_id)
    run = _complete_current(orchestrator, "project_1", run)
    with pytest.raises(InvalidAuditTransitionError):
        orchestrator.start_stage("project_1", run.audit_run_id, AuditStage.REPORTING)
    run = orchestrator.start_stage("project_1", run.audit_run_id, AuditStage.ROUTING)
    run = _complete_current(orchestrator, "project_1", run)
    run = orchestrator.start_stage(
        "project_1", run.audit_run_id, AuditStage.EXECUTING_AGENTS
    )
    with pytest.raises(InvalidAuditTransitionError):
        orchestrator.start_stage("project_1", run.audit_run_id, AuditStage.ROUTING)


def test_completed_stage_retry_is_idempotent_and_sequence_stays_contiguous(tmp_path):
    created = _created(tmp_path)
    orchestrator = AuditOrchestrator(tmp_path)
    running = orchestrator.start("project_1", created.audit_run_id)
    result = StageResult(
        stage="PREPARING",
        success=True,
        summary="Repository preparation state recorded.",
    )
    completed = orchestrator.complete_stage("project_1", created.audit_run_id, result)
    repeated = orchestrator.complete_stage("project_1", created.audit_run_id, result)
    assert repeated.event_count == completed.event_count
    events = list_audit_events(tmp_path, "project_1", created.audit_run_id).events
    assert [event.sequence_number for event in events] == list(range(1, len(events) + 1))
    assert sum(event.event_type == AuditEventType.STAGE_COMPLETED for event in events) == 1
    assert running.event_count + 1 == completed.event_count


def test_failure_records_safe_state_and_two_idempotent_events(tmp_path):
    created = _created(tmp_path)
    orchestrator = AuditOrchestrator(tmp_path)
    orchestrator.start("project_1", created.audit_run_id)
    failed = orchestrator.fail_stage(
        "project_1",
        created.audit_run_id,
        AuditStage.PREPARING,
        error_code="prepare failed!",
        error_message="Trace failed in /tmp/private/repository.py line 10",
    )
    assert failed.status == AuditRunStatus.FAILED
    assert failed.failure_stage == AuditStage.PREPARING
    assert failed.error_code == "PREPARE_FAILED_"
    assert "/tmp/" not in failed.error_message
    events = list_audit_events(tmp_path, "project_1", created.audit_run_id).events
    assert [event.event_type for event in events[-2:]] == [
        AuditEventType.STAGE_FAILED,
        AuditEventType.AUDIT_FAILED,
    ]
    repeated = orchestrator.fail_stage(
        "project_1",
        created.audit_run_id,
        AuditStage.PREPARING,
        error_code="prepare failed!",
        error_message="Trace failed in /tmp/private/repository.py line 10",
    )
    assert repeated.event_count == failed.event_count
    with pytest.raises(InvalidAuditTransitionError):
        orchestrator.start("project_1", created.audit_run_id)


def test_skeleton_run_only_starts_preparing_and_has_no_protocol_side_effects(tmp_path):
    created = _created(tmp_path)
    run = AuditOrchestrator(tmp_path).run("project_1", created.audit_run_id)
    assert run.status == AuditRunStatus.RUNNING
    assert run.current_stage == AuditStage.PREPARING
    assert next(state for state in run.stage_states if state.stage == AuditStage.PREPARING).status == AuditStageStatus.RUNNING
    assert all(
        next(state for state in run.stage_states if state.stage == stage).status
        == AuditStageStatus.PENDING
        for stage in EXECUTABLE_AUDIT_STAGES[1:]
    )
    assert sorted(path.name for path in tmp_path.iterdir()) == ["audit-runs"]
    serialized = run.model_dump_json()
    for forbidden in (
        "submission_id",
        "validation_id",
        "contribution_score_id",
        "finding_cluster_id",
        "reward_event_id",
        "/home/",
        "/tmp/",
    ):
        assert forbidden not in serialized


def test_progress_event_and_sequence_pagination_are_deterministic(tmp_path):
    created = _created(tmp_path)
    orchestrator = AuditOrchestrator(tmp_path)
    orchestrator.start("project_1", created.audit_run_id)
    progressed = orchestrator.update_stage_progress(
        "project_1",
        created.audit_run_id,
        AuditStage.PREPARING,
        progress_current=3,
        progress_total=5,
        summary="3 of 5 preparation checks completed.",
    )
    assert progressed.event_count == 3
    page = list_audit_events(
        tmp_path,
        "project_1",
        created.audit_run_id,
        after_sequence=1,
        limit=1,
    )
    assert page.returned == 1
    assert page.events[0].sequence_number == 2
    assert page.next_after_sequence == 2
    assert page.has_more
    final_page = list_audit_events(
        tmp_path,
        "project_1",
        created.audit_run_id,
        after_sequence=progressed.latest_event_sequence,
    )
    assert final_page.events == []


def test_events_never_leak_between_audit_runs(tmp_path):
    first = _created(tmp_path, "project_1")
    second = _created(tmp_path, "project_1")
    append_audit_event(
        tmp_path,
        "project_1",
        first.audit_run_id,
        event_type=AuditEventType.ROUTING_NODE_SELECTED,
        stage=AuditStage.ROUTING,
        message="Synthetic node selection recorded.",
        node_id="node_17",
        operator_id="operator_4",
        category="reentrancy",
        entity_type="routing_assignment",
        entity_id="assignment_1",
        metadata={"selection_type": "ranked"},
    )
    first_events = list_audit_events(tmp_path, "project_1", first.audit_run_id).events
    second_events = list_audit_events(tmp_path, "project_1", second.audit_run_id).events
    assert len(first_events) == 2
    assert len(second_events) == 1
    assert all(event.audit_run_id == first.audit_run_id for event in first_events)
    assert all(event.audit_run_id == second.audit_run_id for event in second_events)


def test_semantic_event_retry_is_idempotent_and_conflict_is_rejected(tmp_path):
    created = _created(tmp_path)
    kwargs = {
        "event_type": AuditEventType.ROUTING_NODE_SELECTED,
        "stage": AuditStage.ROUTING,
        "message": "Synthetic routing selection recorded.",
        "entity_type": "routing_assignment",
        "entity_id": "assignment_1",
        "node_id": "node_17",
        "metadata": {"position": 1},
        "semantic_key": "routing-node-selected:assignment_1",
    }
    first_run, first_event = append_audit_event(
        tmp_path, "project_1", created.audit_run_id, **kwargs
    )
    second_run, second_event = append_audit_event(
        tmp_path, "project_1", created.audit_run_id, **kwargs
    )
    assert second_event == first_event
    assert second_run.event_count == first_run.event_count
    with pytest.raises(AuditEventConflictError):
        append_audit_event(
            tmp_path,
            "project_1",
            created.audit_run_id,
            **{**kwargs, "metadata": {"position": 2}},
        )


def test_request_fingerprint_is_deterministic_and_input_bound():
    first = build_audit_run_request_fingerprint(
        "project_1", AuditExecutionMode.LOCAL_SIMULATOR, "a" * 64
    )
    assert first == build_audit_run_request_fingerprint(
        "project_1", AuditExecutionMode.LOCAL_SIMULATOR, "a" * 64
    )
    assert first != build_audit_run_request_fingerprint(
        "project_1", AuditExecutionMode.LOCAL_SIMULATOR, "b" * 64
    )


def test_retry_recovers_event_written_before_run_cursor(monkeypatch, tmp_path):
    created = _created(tmp_path)
    orchestrator = AuditOrchestrator(tmp_path)
    original_write = audit_run_service._write_audit_run
    calls = 0

    def interrupt_cursor_write(root, run):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated interruption after event append")
        return original_write(root, run)

    monkeypatch.setattr(audit_run_service, "_write_audit_run", interrupt_cursor_write)
    with pytest.raises(OSError):
        orchestrator.start("project_1", created.audit_run_id)
    monkeypatch.setattr(audit_run_service, "_write_audit_run", original_write)

    recovered = orchestrator.start("project_1", created.audit_run_id)
    assert recovered.status == AuditRunStatus.RUNNING
    assert recovered.current_stage == AuditStage.PREPARING
    assert recovered.event_count == 2
    events = list_audit_events(tmp_path, "project_1", created.audit_run_id).events
    assert [event.event_type for event in events] == [
        AuditEventType.AUDIT_CREATED,
        AuditEventType.STAGE_STARTED,
    ]


def test_frontend_can_reconstruct_realistic_timeline_without_messages(tmp_path):
    created = _created(tmp_path)
    orchestrator = AuditOrchestrator(tmp_path)
    run = orchestrator.start("project_1", created.audit_run_id)
    run = _complete_current(orchestrator, "project_1", run)
    run = orchestrator.start_stage("project_1", run.audit_run_id, AuditStage.ROUTING)
    run = _complete_current(orchestrator, "project_1", run)
    run = orchestrator.start_stage(
        "project_1", run.audit_run_id, AuditStage.EXECUTING_AGENTS
    )
    timeline = list_audit_events(tmp_path, "project_1", run.audit_run_id).events
    assert [event.event_type for event in timeline] == [
        AuditEventType.AUDIT_CREATED,
        AuditEventType.STAGE_STARTED,
        AuditEventType.STAGE_COMPLETED,
        AuditEventType.STAGE_STARTED,
        AuditEventType.STAGE_COMPLETED,
        AuditEventType.STAGE_STARTED,
    ]
    assert run.status == AuditRunStatus.RUNNING
    assert run.current_stage == AuditStage.EXECUTING_AGENTS
    assert run.progress.completed_stage_count == 2
    assert run.latest_event_sequence == 6
    assert [state.stage for state in run.stage_states if state.status == AuditStageStatus.COMPLETED] == [
        AuditStage.PREPARING,
        AuditStage.ROUTING,
    ]


def test_timestamps_are_utc_and_persist_after_reload(tmp_path):
    created = _created(tmp_path)
    reloaded = load_audit_run(tmp_path, "project_1", created.audit_run_id)
    assert reloaded is not None
    assert reloaded.created_at.utcoffset() == timezone.utc.utcoffset(reloaded.created_at)
    assert reloaded.request_fingerprint == created.request_fingerprint
