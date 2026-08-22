from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from app.schemas.audit_run import (
    EXECUTABLE_AUDIT_STAGES,
    AuditEventLevel,
    AuditEventType,
    AuditRun,
    AuditRunStatus,
    AuditStage,
    AuditStageState,
    AuditStageStatus,
    StageResult,
    progress_from_stage_states,
)
from app.services.audit_run_service import (
    AuditRunConflictError,
    apply_audit_transition,
    require_audit_run,
)


class AuditOrchestratorError(AuditRunConflictError):
    pass


class InvalidAuditTransitionError(AuditOrchestratorError):
    pass


class AuditStageExecutionError(AuditOrchestratorError):
    pass


@dataclass(frozen=True)
class AuditStageContext:
    """Minimal correlation context passed to future stage delegates."""

    audit_run_id: str
    project_id: str
    protocol_data_root: Path
    report_progress: Callable[[int, int | None, str], None]


class AuditStageHandler(Protocol):
    """Future business-stage adapter; implementations return references only."""

    def execute(self, context: AuditStageContext) -> StageResult: ...


class AuditCompletionGate(Protocol):
    """Future authoritative downstream terminal-state verification contract."""

    def verify(self, audit_run: AuditRun) -> None: ...


class AuditOrchestrator:
    """Coordinates lifecycle state and delegates all protocol business logic."""

    def __init__(
        self,
        protocol_data_root: Path,
        *,
        stage_handlers: dict[AuditStage, AuditStageHandler] | None = None,
        completion_gate: AuditCompletionGate | None = None,
        stop_after_stage: AuditStage | None = None,
    ) -> None:
        self.protocol_data_root = protocol_data_root
        self.stage_handlers = dict(stage_handlers or {})
        self.completion_gate = completion_gate
        self.stop_after_stage = stop_after_stage
        if AuditStage.COMPLETED in self.stage_handlers:
            raise ValueError("COMPLETED is a lifecycle sentinel, not a stage handler")

    def start(self, project_id: str, audit_run_id: str) -> AuditRun:
        """Start the skeleton at PREPARING without executing a business stage."""
        return self.start_stage(project_id, audit_run_id, AuditStage.PREPARING)

    def transition_stage(
        self,
        project_id: str,
        audit_run_id: str,
        stage: AuditStage,
        **kwargs,
    ) -> AuditRun:
        """Named state-machine entry point retained for future stage adapters."""
        return self.start_stage(project_id, audit_run_id, stage, **kwargs)

    def run(self, project_id: str, audit_run_id: str) -> AuditRun:
        """Run registered handlers; the Day 1 empty registry stops at PREPARING."""
        run = require_audit_run(self.protocol_data_root, project_id, audit_run_id)
        if run.status == AuditRunStatus.CREATED:
            run = self.start(project_id, audit_run_id)
        while run.status == AuditRunStatus.RUNNING:
            stage = run.current_stage
            if stage is None or stage == AuditStage.COMPLETED:
                raise InvalidAuditTransitionError("Running AuditRun has no executable stage")
            state = _state(run, stage)
            if state.status == AuditStageStatus.COMPLETED:
                if stage == self.stop_after_stage:
                    return run
                next_stage = _next_stage(stage)
                if next_stage == AuditStage.COMPLETED:
                    return self.complete_audit(project_id, audit_run_id)
                run = self.start_stage(project_id, audit_run_id, next_stage)
                continue
            handler = self.stage_handlers.get(stage)
            if handler is None:
                return run
            context = AuditStageContext(
                audit_run_id=audit_run_id,
                project_id=project_id,
                protocol_data_root=self.protocol_data_root,
                report_progress=lambda current, total, summary: self.update_stage_progress(
                    project_id,
                    audit_run_id,
                    stage,
                    progress_current=current,
                    progress_total=total,
                    summary=summary,
                ),
            )
            try:
                result = handler.execute(context)
                if not result.success:
                    return self.fail_stage(
                        project_id,
                        audit_run_id,
                        stage,
                        error_code="STAGE_HANDLER_FAILED",
                        error_message=result.summary,
                    )
                run = self.complete_stage(project_id, audit_run_id, result)
            except Exception as exc:
                return self.fail_stage(
                    project_id,
                    audit_run_id,
                    stage,
                    error_code="STAGE_EXECUTION_ERROR",
                    error_message=_safe_error_message(str(exc)),
                )
        return run

    def start_stage(
        self,
        project_id: str,
        audit_run_id: str,
        stage: AuditStage,
        *,
        progress_total: int | None = None,
        summary: str | None = None,
        now: datetime | None = None,
    ) -> AuditRun:
        if stage == AuditStage.COMPLETED:
            raise InvalidAuditTransitionError(
                "COMPLETED is reached through complete_audit"
            )
        run = require_audit_run(self.protocol_data_root, project_id, audit_run_id)
        existing = _state(run, stage)
        if existing.status == AuditStageStatus.RUNNING and run.current_stage == stage:
            return run
        if (
            existing.status in {AuditStageStatus.COMPLETED, AuditStageStatus.SKIPPED}
            and run.current_stage == stage
        ):
            return run
        if existing.status != AuditStageStatus.PENDING:
            raise InvalidAuditTransitionError("A prior stage cannot be restarted")
        _assert_can_start(run, stage)
        attempt = existing.attempt_count + 1

        def transition(current: AuditRun, sequence: int, timestamp: datetime) -> AuditRun:
            _assert_can_start(current, stage)
            states = _replace_state(
                current,
                AuditStageState(
                    stage=stage,
                    status=AuditStageStatus.RUNNING,
                    attempt_count=attempt,
                    started_at=timestamp,
                    progress_current=0,
                    progress_total=progress_total,
                    summary=summary,
                    event_sequence_start=sequence,
                ),
            )
            return AuditRun.model_validate(
                {
                    **current.model_dump(),
                    "status": AuditRunStatus.RUNNING,
                    "current_stage": stage,
                    "started_at": current.started_at or timestamp,
                    "updated_at": timestamp,
                    "stage_states": states,
                    "progress": progress_from_stage_states(states),
                }
            )

        updated, _ = apply_audit_transition(
            self.protocol_data_root,
            project_id,
            audit_run_id,
            transition=transition,
            event_type=AuditEventType.STAGE_STARTED,
            event_level=AuditEventLevel.INFO,
            message=f"Audit stage {stage.value} started.",
            semantic_key=f"stage:{stage.value}:started:attempt:{attempt}",
            stage=stage,
            entity_type="audit_run",
            entity_id=audit_run_id,
            metadata={
                "attempt_count": attempt,
                "progress_current": 0,
                "progress_total": progress_total,
            },
            now=now,
        )
        return updated

    def update_stage_progress(
        self,
        project_id: str,
        audit_run_id: str,
        stage: AuditStage,
        *,
        progress_current: int,
        progress_total: int | None,
        summary: str,
        now: datetime | None = None,
    ) -> AuditRun:
        run = require_audit_run(self.protocol_data_root, project_id, audit_run_id)
        state = _state(run, stage)
        if run.status != AuditRunStatus.RUNNING or state.status != AuditStageStatus.RUNNING:
            raise InvalidAuditTransitionError("Only a running stage can report progress")
        if run.current_stage != stage:
            raise InvalidAuditTransitionError("Progress stage is not current")
        if progress_current < 0 or progress_total is not None and progress_current > progress_total:
            raise InvalidAuditTransitionError("Stage progress is outside its bounds")

        def transition(current: AuditRun, sequence: int, timestamp: datetime) -> AuditRun:
            current_state = _state(current, stage)
            if current_state.status != AuditStageStatus.RUNNING:
                raise InvalidAuditTransitionError("Stage is no longer running")
            progress_state = AuditStageState.model_validate(
                {
                    **current_state.model_dump(),
                    "progress_current": progress_current,
                    "progress_total": progress_total,
                    "summary": summary,
                }
            )
            states = _replace_state(current, progress_state)
            return AuditRun.model_validate(
                {
                    **current.model_dump(),
                    "stage_states": states,
                    "updated_at": timestamp,
                    "progress": progress_from_stage_states(states),
                }
            )

        updated, _ = apply_audit_transition(
            self.protocol_data_root,
            project_id,
            audit_run_id,
            transition=transition,
            event_type=AuditEventType.STAGE_PROGRESS,
            event_level=AuditEventLevel.INFO,
            message=summary,
            semantic_key=(
                f"stage:{stage.value}:progress:attempt:{state.attempt_count}:"
                f"{progress_current}:{progress_total}"
            ),
            stage=stage,
            entity_type="audit_run",
            entity_id=audit_run_id,
            metadata={
                "attempt_count": state.attempt_count,
                "progress_current": progress_current,
                "progress_total": progress_total,
            },
            now=now,
        )
        return updated

    def complete_stage(
        self,
        project_id: str,
        audit_run_id: str,
        result: StageResult,
        *,
        now: datetime | None = None,
    ) -> AuditRun:
        if result.stage == AuditStage.COMPLETED:
            raise InvalidAuditTransitionError("COMPLETED is not an executable stage")
        if not result.success:
            raise AuditStageExecutionError("Failed StageResult must use fail_stage")
        run = require_audit_run(self.protocol_data_root, project_id, audit_run_id)
        state = _state(run, result.stage)
        if state.status == AuditStageStatus.COMPLETED:
            return run
        if (
            run.status != AuditRunStatus.RUNNING
            or run.current_stage != result.stage
            or state.status != AuditStageStatus.RUNNING
        ):
            raise InvalidAuditTransitionError("Only the current running stage can complete")
        if any(ref.audit_run_id != audit_run_id for ref in result.artifact_refs):
            raise AuditStageExecutionError("StageResult artifact belongs to another AuditRun")

        def transition(current: AuditRun, sequence: int, timestamp: datetime) -> AuditRun:
            current_state = _state(current, result.stage)
            if current_state.status != AuditStageStatus.RUNNING:
                raise InvalidAuditTransitionError("Stage is no longer running")
            completed_state = AuditStageState.model_validate(
                {
                    **current_state.model_dump(),
                    "status": AuditStageStatus.COMPLETED,
                    "completed_at": timestamp,
                    "progress_current": result.progress_current,
                    "progress_total": result.progress_total,
                    "summary": result.summary,
                    "artifact_refs": result.artifact_refs,
                    "event_sequence_end": sequence,
                }
            )
            states = _replace_state(current, completed_state)
            artifacts = _merge_artifacts(current.artifact_refs, result.artifact_refs)
            return AuditRun.model_validate(
                {
                    **current.model_dump(),
                    "stage_states": states,
                    "artifact_refs": artifacts,
                    "updated_at": timestamp,
                    "progress": progress_from_stage_states(states),
                }
            )

        entity_refs = [ref.model_dump(mode="json") for ref in result.entity_refs]
        updated, _ = apply_audit_transition(
            self.protocol_data_root,
            project_id,
            audit_run_id,
            transition=transition,
            event_type=AuditEventType.STAGE_COMPLETED,
            event_level=AuditEventLevel.INFO,
            message=result.summary,
            semantic_key=(
                f"stage:{result.stage.value}:completed:attempt:{state.attempt_count}"
            ),
            stage=result.stage,
            entity_type="audit_run",
            entity_id=audit_run_id,
            metadata={
                "attempt_count": state.attempt_count,
                "progress_current": result.progress_current,
                "progress_total": result.progress_total,
                "entity_refs": entity_refs,
                "stage_metadata": result.metadata,
            },
            artifact_refs=result.artifact_refs,
            now=now,
        )
        return updated

    def fail_stage(
        self,
        project_id: str,
        audit_run_id: str,
        stage: AuditStage,
        *,
        error_code: str,
        error_message: str,
        now: datetime | None = None,
    ) -> AuditRun:
        safe_code = _safe_error_code(error_code)
        safe_message = _safe_error_message(error_message)
        run = require_audit_run(self.protocol_data_root, project_id, audit_run_id)
        if run.status == AuditRunStatus.COMPLETED:
            raise InvalidAuditTransitionError("Completed AuditRun cannot fail")
        if run.status != AuditRunStatus.FAILED:
            state = _state(run, stage)
            if (
                run.status != AuditRunStatus.RUNNING
                or run.current_stage != stage
                or state.status != AuditStageStatus.RUNNING
            ):
                raise InvalidAuditTransitionError("Only the current running stage can fail")

            def fail_transition(
                current: AuditRun,
                sequence: int,
                timestamp: datetime,
            ) -> AuditRun:
                current_state = _state(current, stage)
                failed_state = AuditStageState.model_validate(
                    {
                        **current_state.model_dump(),
                        "status": AuditStageStatus.FAILED,
                        "completed_at": timestamp,
                        "error_code": safe_code,
                        "error_message": safe_message,
                        "event_sequence_end": sequence,
                    }
                )
                states = _replace_state(current, failed_state)
                return AuditRun.model_validate(
                    {
                        **current.model_dump(),
                        "status": AuditRunStatus.FAILED,
                        "current_stage": stage,
                        "failed_at": timestamp,
                        "failure_stage": stage,
                        "error_code": safe_code,
                        "error_message": safe_message,
                        "updated_at": timestamp,
                        "stage_states": states,
                        "progress": progress_from_stage_states(states),
                    }
                )

            run, _ = apply_audit_transition(
                self.protocol_data_root,
                project_id,
                audit_run_id,
                transition=fail_transition,
                event_type=AuditEventType.STAGE_FAILED,
                event_level=AuditEventLevel.ERROR,
                message=f"Audit stage {stage.value} failed: {safe_message}",
                semantic_key=(
                    f"stage:{stage.value}:failed:attempt:{state.attempt_count}"
                ),
                stage=stage,
                entity_type="audit_run",
                entity_id=audit_run_id,
                metadata={
                    "attempt_count": state.attempt_count,
                    "error_code": safe_code,
                    "error_message": safe_message,
                },
                now=now,
            )
        elif run.failure_stage != stage:
            raise InvalidAuditTransitionError("AuditRun already failed in another stage")

        def unchanged_failed(
            current: AuditRun,
            sequence: int,
            timestamp: datetime,
        ) -> AuditRun:
            return AuditRun.model_validate(
                {**current.model_dump(), "updated_at": timestamp}
            )

        run, _ = apply_audit_transition(
            self.protocol_data_root,
            project_id,
            audit_run_id,
            transition=unchanged_failed,
            event_type=AuditEventType.AUDIT_FAILED,
            event_level=AuditEventLevel.ERROR,
            message=f"AuditRun failed during {stage.value}: {safe_message}",
            semantic_key=f"audit-failed:{stage.value}",
            stage=stage,
            entity_type="audit_run",
            entity_id=audit_run_id,
            metadata={
                "failure_stage": stage.value,
                "error_code": safe_code,
                "error_message": safe_message,
            },
            now=now,
        )
        return run

    def fail_audit(
        self,
        project_id: str,
        audit_run_id: str,
        *,
        error_code: str,
        error_message: str,
        now: datetime | None = None,
    ) -> AuditRun:
        run = require_audit_run(self.protocol_data_root, project_id, audit_run_id)
        if run.current_stage is None or run.current_stage == AuditStage.COMPLETED:
            raise InvalidAuditTransitionError(
                "AuditRun must have a current executable stage before failure"
            )
        return self.fail_stage(
            project_id,
            audit_run_id,
            run.current_stage,
            error_code=error_code,
            error_message=error_message,
            now=now,
        )

    def complete_audit(
        self,
        project_id: str,
        audit_run_id: str,
        *,
        now: datetime | None = None,
    ) -> AuditRun:
        run = require_audit_run(self.protocol_data_root, project_id, audit_run_id)
        if run.status == AuditRunStatus.COMPLETED:
            return run
        if run.status != AuditRunStatus.RUNNING:
            raise InvalidAuditTransitionError("Only a running AuditRun can complete")
        if any(
            _state(run, stage).status
            not in {AuditStageStatus.COMPLETED, AuditStageStatus.SKIPPED}
            for stage in EXECUTABLE_AUDIT_STAGES
        ):
            raise InvalidAuditTransitionError(
                "Every executable stage must complete before AuditRun completion"
            )
        if self.completion_gate is not None:
            self.completion_gate.verify(run)

        def transition(current: AuditRun, sequence: int, timestamp: datetime) -> AuditRun:
            sentinel = _state(current, AuditStage.COMPLETED)
            completed_sentinel = AuditStageState(
                stage=AuditStage.COMPLETED,
                status=AuditStageStatus.COMPLETED,
                attempt_count=1,
                started_at=timestamp,
                completed_at=timestamp,
                progress_current=1,
                progress_total=1,
                summary="Audit lifecycle completed.",
                event_sequence_start=sequence,
                event_sequence_end=sequence,
                artifact_refs=sentinel.artifact_refs,
            )
            states = _replace_state(current, completed_sentinel)
            return AuditRun.model_validate(
                {
                    **current.model_dump(),
                    "status": AuditRunStatus.COMPLETED,
                    "current_stage": AuditStage.COMPLETED,
                    "completed_at": timestamp,
                    "updated_at": timestamp,
                    "stage_states": states,
                    "progress": progress_from_stage_states(states),
                }
            )

        completed, _ = apply_audit_transition(
            self.protocol_data_root,
            project_id,
            audit_run_id,
            transition=transition,
            event_type=AuditEventType.AUDIT_COMPLETED,
            event_level=AuditEventLevel.INFO,
            message="AuditRun completed.",
            semantic_key="audit-completed",
            stage=AuditStage.COMPLETED,
            entity_type="audit_run",
            entity_id=audit_run_id,
            metadata={
                "completed_stage_count": len(EXECUTABLE_AUDIT_STAGES),
                "total_stage_count": len(EXECUTABLE_AUDIT_STAGES),
            },
            now=now,
        )
        return completed

    def complete_run(
        self,
        project_id: str,
        audit_run_id: str,
        *,
        now: datetime | None = None,
    ) -> AuditRun:
        return self.complete_audit(project_id, audit_run_id, now=now)

    def fail_run(
        self,
        project_id: str,
        audit_run_id: str,
        *,
        error_code: str,
        error_message: str,
        now: datetime | None = None,
    ) -> AuditRun:
        return self.fail_audit(
            project_id,
            audit_run_id,
            error_code=error_code,
            error_message=error_message,
            now=now,
        )


def _assert_can_start(run: AuditRun, stage: AuditStage) -> None:
    if run.status in {AuditRunStatus.COMPLETED, AuditRunStatus.FAILED}:
        raise InvalidAuditTransitionError("Terminal AuditRun cannot start a stage")
    index = EXECUTABLE_AUDIT_STAGES.index(stage)
    if index == 0:
        if run.status != AuditRunStatus.CREATED or run.current_stage is not None:
            raise InvalidAuditTransitionError("PREPARING must be the first stage")
        return
    if run.status != AuditRunStatus.RUNNING:
        raise InvalidAuditTransitionError("Later stages require a running AuditRun")
    previous = EXECUTABLE_AUDIT_STAGES[index - 1]
    if run.current_stage != previous:
        raise InvalidAuditTransitionError(
            f"{stage.value} must follow {previous.value}"
        )
    if _state(run, previous).status not in {
        AuditStageStatus.COMPLETED,
        AuditStageStatus.SKIPPED,
    }:
        raise InvalidAuditTransitionError(
            f"{previous.value} must complete before {stage.value}"
        )
    if any(
        _state(run, required).status
        not in {AuditStageStatus.COMPLETED, AuditStageStatus.SKIPPED}
        for required in EXECUTABLE_AUDIT_STAGES[:index]
    ):
        raise InvalidAuditTransitionError("Required prior stage is incomplete")


def _state(run: AuditRun, stage: AuditStage) -> AuditStageState:
    return next(state for state in run.stage_states if state.stage == stage)


def _replace_state(run: AuditRun, replacement: AuditStageState) -> list[AuditStageState]:
    return [
        replacement if state.stage == replacement.stage else state
        for state in run.stage_states
    ]


def _next_stage(stage: AuditStage) -> AuditStage:
    index = list(AuditStage).index(stage)
    return list(AuditStage)[index + 1]


def _merge_artifacts(existing, additions):
    by_id = {ref.artifact_id: ref for ref in existing}
    for ref in additions:
        previous = by_id.get(ref.artifact_id)
        if previous is not None and previous != ref:
            raise AuditStageExecutionError("Artifact identifier has conflicting content")
        by_id[ref.artifact_id] = ref
    return [by_id[key] for key in sorted(by_id)]


def _safe_error_code(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", value.strip().upper())[:128]
    return cleaned or "STAGE_ERROR"


def _safe_error_message(value: str) -> str:
    compact = " ".join(value.split())
    compact = re.sub(r"(?:[A-Za-z]:\\|/)[^\s]+", "[redacted-path]", compact)
    return (compact or "Stage execution failed.")[:1000]
