from __future__ import annotations

import json
import math
import re
from datetime import datetime
from enum import Enum
from pathlib import PurePath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


AUDIT_RUN_VERSION = "audit_run_v1"
AUDIT_EVENT_VERSION = "audit_event_v1"
MAX_EVENT_METADATA_BYTES = 32 * 1024
MAX_METADATA_DEPTH = 8
MAX_METADATA_ITEMS = 1000
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,255}$")
SAFE_STORAGE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,511}$")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


class AuditRunStatus(str, Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class AuditExecutionMode(str, Enum):
    LOCAL_SIMULATOR = "local_simulator"
    REMOTE_NETWORK = "remote_network"


class AuditStage(str, Enum):
    PREPARING = "PREPARING"
    ROUTING = "ROUTING"
    EXECUTING_AGENTS = "EXECUTING_AGENTS"
    REPRODUCING = "REPRODUCING"
    VALIDATING = "VALIDATING"
    CALIBRATING = "CALIBRATING"
    CLUSTERING = "CLUSTERING"
    ASSESSING_QUALITY = "ASSESSING_QUALITY"
    REWARDING = "REWARDING"
    REPORTING = "REPORTING"
    COMPLETED = "COMPLETED"


EXECUTABLE_AUDIT_STAGES: tuple[AuditStage, ...] = tuple(
    stage for stage in AuditStage if stage != AuditStage.COMPLETED
)


class AuditStageStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class AuditEventType(str, Enum):
    AUDIT_CREATED = "AUDIT_CREATED"
    STAGE_STARTED = "STAGE_STARTED"
    STAGE_PROGRESS = "STAGE_PROGRESS"
    STAGE_COMPLETED = "STAGE_COMPLETED"
    STAGE_FAILED = "STAGE_FAILED"
    ARTIFACT_CREATED = "ARTIFACT_CREATED"
    AUDIT_COMPLETED = "AUDIT_COMPLETED"
    AUDIT_FAILED = "AUDIT_FAILED"

    ROUTING_NODE_SELECTED = "ROUTING_NODE_SELECTED"
    ROUTING_ASSIGNMENT_CREATED = "ROUTING_ASSIGNMENT_CREATED"
    AGENT_EXECUTION_STARTED = "AGENT_EXECUTION_STARTED"
    AGENT_EXECUTION_COMPLETED = "AGENT_EXECUTION_COMPLETED"
    AGENT_EXECUTION_FAILED = "AGENT_EXECUTION_FAILED"
    FINDING_CREATED = "FINDING_CREATED"
    SUBMISSION_CREATED = "SUBMISSION_CREATED"
    REPRODUCTION_STARTED = "REPRODUCTION_STARTED"
    REPRODUCTION_LOG = "REPRODUCTION_LOG"
    REPRODUCTION_COMPLETED = "REPRODUCTION_COMPLETED"
    VALIDATION_COMPLETED = "VALIDATION_COMPLETED"
    CONTRIBUTION_SCORE_CALCULATED = "CONTRIBUTION_SCORE_CALCULATED"
    REPUTATION_UPDATED = "REPUTATION_UPDATED"
    CATEGORY_SCORE_UPDATED = "CATEGORY_SCORE_UPDATED"
    MEMBERSHIP_UPDATED = "MEMBERSHIP_UPDATED"
    FINDING_CLUSTER_CREATED = "FINDING_CLUSTER_CREATED"
    QUALITY_ASSESSMENT_COMPLETED = "QUALITY_ASSESSMENT_COMPLETED"
    REWARD_CLUSTER_CALCULATED = "REWARD_CLUSTER_CALCULATED"
    TOP_K_SELECTED = "TOP_K_SELECTED"
    CHIEF_FINDER_SELECTED = "CHIEF_FINDER_SELECTED"
    OPERATOR_REWARD_CALCULATED = "OPERATOR_REWARD_CALCULATED"
    REWARD_CYCLE_FINALIZED = "REWARD_CYCLE_FINALIZED"
    REWARD_EVENT_CREATED = "REWARD_EVENT_CREATED"
    REPORT_GENERATED = "REPORT_GENERATED"


class AuditEventLevel(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class AuditArtifactType(str, Enum):
    FOUNDRY_STDOUT = "FOUNDRY_STDOUT"
    FOUNDRY_STDERR = "FOUNDRY_STDERR"
    DOCKER_LOG = "DOCKER_LOG"
    POC_SOURCE = "POC_SOURCE"
    VALIDATION_EVIDENCE = "VALIDATION_EVIDENCE"
    REWARD_CALCULATION = "REWARD_CALCULATION"
    FINAL_REPORT_JSON = "FINAL_REPORT_JSON"
    FINAL_REPORT_MARKDOWN = "FINAL_REPORT_MARKDOWN"


def validate_audit_identifier(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or not SAFE_IDENTIFIER.fullmatch(value)
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or PurePath(value).is_absolute()
    ):
        raise ValueError(f"Invalid {label} identifier")
    return value


def _aware(value: datetime | None, label: str) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError(f"{label} must be timezone-aware")
    return value


def _safe_public_text(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{label} must not be empty")
    lowered = cleaned.lower()
    if (
        lowered.startswith(("/", "~/"))
        or "/home/" in lowered
        or "/tmp/" in lowered
        or re.search(r"(?:^|\s)[a-zA-Z]:\\", cleaned)
    ):
        raise ValueError(f"{label} must not expose a host filesystem path")
    return cleaned


def _validate_json_value(value: Any, *, depth: int = 0) -> None:
    if depth > MAX_METADATA_DEPTH:
        raise ValueError("Audit event metadata nesting is too deep")
    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, str):
            if len(value) > 4096:
                raise ValueError("Audit event metadata strings are too large")
            _safe_public_text(value, "Audit event metadata value")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Audit event metadata numbers must be finite")
        return
    if isinstance(value, list):
        if len(value) > MAX_METADATA_ITEMS:
            raise ValueError("Audit event metadata arrays are too large")
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > MAX_METADATA_ITEMS:
            raise ValueError("Audit event metadata objects are too large")
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 128:
                raise ValueError("Audit event metadata keys must be bounded strings")
            if key.lower() in {"host_path", "filesystem_path", "private_key"}:
                raise ValueError("Audit event metadata contains a forbidden field")
            _validate_json_value(item, depth=depth + 1)
        return
    raise ValueError("Audit event metadata must contain only JSON-safe values")


def validate_event_metadata(value: dict[str, Any]) -> dict[str, Any]:
    _validate_json_value(value)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("Audit event metadata must be JSON-safe") from exc
    if len(encoded) > MAX_EVENT_METADATA_BYTES:
        raise ValueError("Audit event metadata exceeds the size limit")
    return value


class AuditEntityRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    entity_type: str = Field(..., min_length=1, max_length=128)
    entity_id: str = Field(..., min_length=1, max_length=256)

    @field_validator("entity_type", "entity_id")
    @classmethod
    def validate_fields(cls, value: str, info) -> str:
        return validate_audit_identifier(value, info.field_name)


class AuditArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str = Field(..., min_length=1, max_length=256)
    audit_run_id: str = Field(..., min_length=1, max_length=256)
    artifact_type: AuditArtifactType
    entity_type: str | None = Field(default=None, min_length=1, max_length=128)
    entity_id: str | None = Field(default=None, min_length=1, max_length=256)
    display_name: str = Field(..., min_length=1, max_length=256)
    content_type: str = Field(..., min_length=1, max_length=128)
    storage_ref: str = Field(..., min_length=1, max_length=512)
    size_bytes: int = Field(..., ge=0)
    sha256: str
    created_at: datetime

    @field_validator("artifact_id", "audit_run_id", "entity_type", "entity_id")
    @classmethod
    def validate_identifiers(cls, value: str | None, info) -> str | None:
        return (
            None
            if value is None
            else validate_audit_identifier(value, info.field_name)
        )

    @field_validator("display_name", "content_type")
    @classmethod
    def validate_public_text(cls, value: str, info) -> str:
        cleaned = _safe_public_text(value, info.field_name)
        assert cleaned is not None
        if info.field_name == "display_name" and ("/" in cleaned or "\\" in cleaned):
            raise ValueError("Artifact display_name must not contain a path")
        return cleaned

    @field_validator("storage_ref")
    @classmethod
    def validate_storage_ref(cls, value: str) -> str:
        if (
            not SAFE_STORAGE_REF.fullmatch(value)
            or value.startswith(("/", "."))
            or "//" in value
            or "\\" in value
            or ":" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ValueError("Artifact storage_ref must be a safe logical reference")
        return value

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if not SHA256_HEX.fullmatch(value):
            raise ValueError("Artifact sha256 must be a lowercase SHA-256 digest")
        return value

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        return _aware(value, "Artifact created_at")  # type: ignore[return-value]

    @model_validator(mode="after")
    def validate_entity_pair(self) -> "AuditArtifactRef":
        if (self.entity_type is None) != (self.entity_id is None):
            raise ValueError("Artifact entity_type and entity_id must be set together")
        return self


class AuditStageState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: AuditStage
    status: AuditStageStatus = AuditStageStatus.PENDING
    attempt_count: int = Field(default=0, ge=0)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    progress_current: int = Field(default=0, ge=0)
    progress_total: int | None = Field(default=None, ge=0)
    summary: str | None = Field(default=None, max_length=1000)
    error_code: str | None = Field(default=None, min_length=1, max_length=128)
    error_message: str | None = Field(default=None, min_length=1, max_length=1000)
    artifact_refs: list[AuditArtifactRef] = Field(default_factory=list, max_length=1000)
    event_sequence_start: int | None = Field(default=None, ge=1)
    event_sequence_end: int | None = Field(default=None, ge=1)

    @field_validator("summary", "error_message")
    @classmethod
    def validate_safe_text(cls, value: str | None, info) -> str | None:
        return _safe_public_text(value, info.field_name)

    @field_validator("error_code")
    @classmethod
    def validate_error_code(cls, value: str | None) -> str | None:
        return (
            None
            if value is None
            else validate_audit_identifier(value, "stage error code")
        )

    @field_validator("started_at", "completed_at")
    @classmethod
    def validate_timestamps(cls, value: datetime | None, info) -> datetime | None:
        return _aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_stage_state(self) -> "AuditStageState":
        if (
            self.progress_total is not None
            and self.progress_current > self.progress_total
        ):
            raise ValueError("Stage progress_current cannot exceed progress_total")
        if self.status == AuditStageStatus.PENDING:
            if self.started_at is not None or self.completed_at is not None:
                raise ValueError("Pending stage cannot have lifecycle timestamps")
            if self.attempt_count != 0:
                raise ValueError("Pending stage cannot have attempts")
        elif self.status == AuditStageStatus.RUNNING:
            if self.started_at is None or self.completed_at is not None:
                raise ValueError("Running stage requires started_at only")
            if self.attempt_count < 1:
                raise ValueError("Running stage requires an attempt")
        elif self.status in {AuditStageStatus.COMPLETED, AuditStageStatus.SKIPPED}:
            if self.started_at is None or self.completed_at is None:
                raise ValueError("Terminal successful stage requires timestamps")
            if self.attempt_count < 1:
                raise ValueError("Terminal stage requires an attempt")
            if self.error_code is not None or self.error_message is not None:
                raise ValueError("Successful stage cannot contain failure metadata")
        elif self.status == AuditStageStatus.FAILED:
            if self.started_at is None or self.completed_at is None:
                raise ValueError("Failed stage requires lifecycle timestamps")
            if self.attempt_count < 1:
                raise ValueError("Failed stage requires an attempt")
            if self.error_code is None or self.error_message is None:
                raise ValueError("Failed stage requires safe error metadata")
        if self.error_code is not None and self.status != AuditStageStatus.FAILED:
            raise ValueError("Only failed stages may contain error metadata")
        if self.event_sequence_end is not None and self.event_sequence_start is None:
            raise ValueError("Stage event end requires an event start")
        if (
            self.event_sequence_start is not None
            and self.event_sequence_end is not None
            and self.event_sequence_end < self.event_sequence_start
        ):
            raise ValueError("Stage event sequence range is invalid")
        if (
            self.started_at is not None
            and self.completed_at is not None
            and self.completed_at < self.started_at
        ):
            raise ValueError("Stage completed_at cannot precede started_at")
        if self.artifact_refs and any(
            ref.audit_run_id != self.artifact_refs[0].audit_run_id
            for ref in self.artifact_refs
        ):
            raise ValueError("Stage artifacts must belong to one AuditRun")
        return self


class AuditProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    completed_stage_count: int = Field(..., ge=0)
    total_stage_count: int = Field(..., ge=1)
    progress_percentage: float = Field(..., ge=0, le=100)

    @model_validator(mode="after")
    def validate_counts(self) -> "AuditProgress":
        if self.completed_stage_count > self.total_stage_count:
            raise ValueError("Completed stage count cannot exceed total stage count")
        expected = round(
            self.completed_stage_count * 100 / self.total_stage_count,
            2,
        )
        if self.progress_percentage != expected:
            raise ValueError("Audit progress percentage must be server-derived")
        return self


def progress_from_stage_states(stage_states: list[AuditStageState]) -> AuditProgress:
    executable = [state for state in stage_states if state.stage in EXECUTABLE_AUDIT_STAGES]
    completed = sum(
        state.status in {AuditStageStatus.COMPLETED, AuditStageStatus.SKIPPED}
        for state in executable
    )
    total = len(EXECUTABLE_AUDIT_STAGES)
    return AuditProgress(
        completed_stage_count=completed,
        total_stage_count=total,
        progress_percentage=round(completed * 100 / total, 2),
    )


def initial_stage_states() -> list[AuditStageState]:
    return [AuditStageState(stage=stage) for stage in AuditStage]


class AuditRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audit_run_id: str = Field(..., min_length=1, max_length=256)
    audit_run_version: Literal["audit_run_v1"] = AUDIT_RUN_VERSION
    project_id: str = Field(..., min_length=1, max_length=256)
    status: AuditRunStatus
    current_stage: AuditStage | None = None
    execution_mode: AuditExecutionMode
    routing_id: str | None = None
    task_reward_budget_id: str | None = None
    reward_cycle_id: str | None = None
    report_id: str | None = None
    request_fingerprint: str
    progress: AuditProgress
    created_at: datetime
    started_at: datetime | None = None
    updated_at: datetime
    completed_at: datetime | None = None
    failed_at: datetime | None = None
    failure_stage: AuditStage | None = None
    error_code: str | None = Field(default=None, min_length=1, max_length=128)
    error_message: str | None = Field(default=None, min_length=1, max_length=1000)
    stage_states: list[AuditStageState]
    artifact_refs: list[AuditArtifactRef] = Field(default_factory=list, max_length=1000)
    event_count: int = Field(default=0, ge=0)
    latest_event_sequence: int = Field(default=0, ge=0)

    @field_validator(
        "audit_run_id",
        "project_id",
        "routing_id",
        "task_reward_budget_id",
        "reward_cycle_id",
        "report_id",
    )
    @classmethod
    def validate_identifiers(cls, value: str | None, info) -> str | None:
        return (
            None
            if value is None
            else validate_audit_identifier(value, info.field_name)
        )

    @field_validator("request_fingerprint")
    @classmethod
    def validate_fingerprint(cls, value: str) -> str:
        if not SHA256_HEX.fullmatch(value):
            raise ValueError("AuditRun request_fingerprint must be lowercase SHA-256")
        return value

    @field_validator("error_code")
    @classmethod
    def validate_error_code(cls, value: str | None) -> str | None:
        return None if value is None else validate_audit_identifier(value, "error code")

    @field_validator("error_message")
    @classmethod
    def validate_error_message(cls, value: str | None) -> str | None:
        return _safe_public_text(value, "AuditRun error_message")

    @field_validator(
        "created_at", "started_at", "updated_at", "completed_at", "failed_at"
    )
    @classmethod
    def validate_timestamps(cls, value: datetime | None, info) -> datetime | None:
        return _aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "AuditRun":
        expected_stages = list(AuditStage)
        actual_stages = [state.stage for state in self.stage_states]
        if actual_stages != expected_stages:
            raise ValueError("AuditRun stage_states must contain every stage in order")
        if self.progress != progress_from_stage_states(self.stage_states):
            raise ValueError("AuditRun progress must match stage states")
        if self.event_count != self.latest_event_sequence:
            raise ValueError("AuditRun events must use a contiguous sequence")
        if self.updated_at < self.created_at:
            raise ValueError("AuditRun updated_at cannot precede created_at")
        if self.started_at is not None and self.started_at < self.created_at:
            raise ValueError("AuditRun started_at cannot precede created_at")
        if self.completed_at is not None and (
            self.started_at is None or self.completed_at < self.started_at
        ):
            raise ValueError("AuditRun completed_at cannot precede started_at")
        if self.failed_at is not None and (
            self.started_at is None or self.failed_at < self.started_at
        ):
            raise ValueError("AuditRun failed_at cannot precede started_at")
        if self.completed_at is not None and self.updated_at < self.completed_at:
            raise ValueError("AuditRun updated_at cannot precede completed_at")
        if self.failed_at is not None and self.updated_at < self.failed_at:
            raise ValueError("AuditRun updated_at cannot precede failed_at")

        states = {state.stage: state for state in self.stage_states}
        if self.status == AuditRunStatus.CREATED:
            if self.current_stage is not None or self.started_at is not None:
                raise ValueError("Created AuditRun cannot have a current stage or start time")
            if self.completed_at is not None or self.failed_at is not None:
                raise ValueError("Created AuditRun cannot have terminal timestamps")
            if any(state.status != AuditStageStatus.PENDING for state in self.stage_states):
                raise ValueError("Created AuditRun requires pending stage states")
        elif self.status == AuditRunStatus.RUNNING:
            if self.started_at is None or self.current_stage in {None, AuditStage.COMPLETED}:
                raise ValueError("Running AuditRun requires an executable current stage")
            if self.completed_at is not None or self.failed_at is not None:
                raise ValueError("Running AuditRun cannot have terminal timestamps")
            if states[self.current_stage].status not in {
                AuditStageStatus.RUNNING,
                AuditStageStatus.COMPLETED,
                AuditStageStatus.SKIPPED,
            }:
                raise ValueError("AuditRun current stage state is inconsistent")
            current_index = EXECUTABLE_AUDIT_STAGES.index(self.current_stage)
            if any(
                states[stage].status
                not in {AuditStageStatus.COMPLETED, AuditStageStatus.SKIPPED}
                for stage in EXECUTABLE_AUDIT_STAGES[:current_index]
            ):
                raise ValueError("Running AuditRun has an incomplete prior stage")
            if any(
                states[stage].status != AuditStageStatus.PENDING
                for stage in EXECUTABLE_AUDIT_STAGES[current_index + 1 :]
            ) or states[AuditStage.COMPLETED].status != AuditStageStatus.PENDING:
                raise ValueError("Running AuditRun has state beyond its current stage")
        elif self.status == AuditRunStatus.COMPLETED:
            if self.current_stage != AuditStage.COMPLETED or self.completed_at is None:
                raise ValueError("Completed AuditRun requires COMPLETED stage and timestamp")
            if self.failed_at is not None or self.failure_stage is not None:
                raise ValueError("Completed AuditRun cannot contain failure state")
            if any(
                states[stage].status
                not in {AuditStageStatus.COMPLETED, AuditStageStatus.SKIPPED}
                for stage in EXECUTABLE_AUDIT_STAGES
            ):
                raise ValueError("Completed AuditRun requires every executable stage complete")
            if states[AuditStage.COMPLETED].status != AuditStageStatus.COMPLETED:
                raise ValueError("Completed AuditRun requires completed sentinel state")
        elif self.status == AuditRunStatus.FAILED:
            if (
                self.failed_at is None
                or self.failure_stage is None
                or self.error_code is None
                or self.error_message is None
            ):
                raise ValueError("Failed AuditRun requires safe failure information")
            if self.failure_stage == AuditStage.COMPLETED:
                raise ValueError("AuditRun cannot fail in the COMPLETED sentinel")
            if self.current_stage != self.failure_stage:
                raise ValueError("Failed AuditRun current stage must equal failure stage")
            if states[self.failure_stage].status != AuditStageStatus.FAILED:
                raise ValueError("Failed AuditRun requires a failed stage state")
            if self.completed_at is not None:
                raise ValueError("Failed AuditRun cannot have completed_at")
            failure_index = EXECUTABLE_AUDIT_STAGES.index(self.failure_stage)
            if any(
                states[stage].status
                not in {AuditStageStatus.COMPLETED, AuditStageStatus.SKIPPED}
                for stage in EXECUTABLE_AUDIT_STAGES[:failure_index]
            ):
                raise ValueError("Failed AuditRun has an incomplete prior stage")
            if any(
                states[stage].status != AuditStageStatus.PENDING
                for stage in EXECUTABLE_AUDIT_STAGES[failure_index + 1 :]
            ) or states[AuditStage.COMPLETED].status != AuditStageStatus.PENDING:
                raise ValueError("Failed AuditRun has state beyond its failure stage")

        if self.status != AuditRunStatus.FAILED and any(
            item is not None
            for item in (self.failed_at, self.failure_stage, self.error_code, self.error_message)
        ):
            raise ValueError("Only failed AuditRuns may contain failure information")
        for ref in self.artifact_refs:
            if ref.audit_run_id != self.audit_run_id:
                raise ValueError("AuditRun artifact belongs to another run")
        for state in self.stage_states:
            if any(ref.audit_run_id != self.audit_run_id for ref in state.artifact_refs):
                raise ValueError("Stage artifact belongs to another AuditRun")
        return self


class AuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    audit_event_id: str = Field(..., min_length=1, max_length=256)
    event_version: Literal["audit_event_v1"] = AUDIT_EVENT_VERSION
    audit_run_id: str = Field(..., min_length=1, max_length=256)
    sequence_number: int = Field(..., ge=1)
    event_type: AuditEventType
    stage: AuditStage | None = None
    event_level: AuditEventLevel = AuditEventLevel.INFO
    message: str = Field(..., min_length=1, max_length=1000)
    entity_type: str | None = Field(default=None, min_length=1, max_length=128)
    entity_id: str | None = Field(default=None, min_length=1, max_length=256)
    node_id: str | None = Field(default=None, min_length=1, max_length=256)
    operator_id: str | None = Field(default=None, min_length=1, max_length=256)
    category: str | None = Field(default=None, min_length=1, max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)
    artifact_refs: list[AuditArtifactRef] = Field(default_factory=list, max_length=100)
    created_at: datetime

    @field_validator(
        "audit_event_id",
        "audit_run_id",
        "entity_type",
        "entity_id",
        "node_id",
        "operator_id",
        "category",
    )
    @classmethod
    def validate_identifiers(cls, value: str | None, info) -> str | None:
        return (
            None
            if value is None
            else validate_audit_identifier(value, info.field_name)
        )

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        cleaned = _safe_public_text(value, "AuditEvent message")
        assert cleaned is not None
        return cleaned

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_event_metadata(value)

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        return _aware(value, "AuditEvent created_at")  # type: ignore[return-value]

    @model_validator(mode="after")
    def validate_relationships(self) -> "AuditEvent":
        if (self.entity_type is None) != (self.entity_id is None):
            raise ValueError("AuditEvent entity_type and entity_id must be set together")
        if any(ref.audit_run_id != self.audit_run_id for ref in self.artifact_refs):
            raise ValueError("AuditEvent artifact belongs to another AuditRun")
        return self


class StageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: AuditStage
    success: bool
    summary: str = Field(..., min_length=1, max_length=1000)
    progress_current: int = Field(default=1, ge=0)
    progress_total: int | None = Field(default=1, ge=0)
    artifact_refs: list[AuditArtifactRef] = Field(default_factory=list, max_length=1000)
    entity_refs: list[AuditEntityRef] = Field(default_factory=list, max_length=1000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        cleaned = _safe_public_text(value, "StageResult summary")
        assert cleaned is not None
        return cleaned

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_event_metadata(value)

    @model_validator(mode="after")
    def validate_result(self) -> "StageResult":
        if self.progress_total is not None and self.progress_current > self.progress_total:
            raise ValueError("StageResult progress_current cannot exceed progress_total")
        return self


class AuditRunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_mode: Literal[AuditExecutionMode.LOCAL_SIMULATOR] = (
        AuditExecutionMode.LOCAL_SIMULATOR
    )


class AuditRunActionRequest(BaseModel):
    """Empty action body so clients cannot forge lifecycle state."""

    model_config = ConfigDict(extra="forbid")


class AuditRunListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    total: int = Field(..., ge=0)
    audit_runs: list[AuditRun]


class AuditEventListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audit_run_id: str
    after_sequence: int = Field(..., ge=0)
    limit: int = Field(..., ge=1, le=200)
    returned: int = Field(..., ge=0)
    latest_event_sequence: int = Field(..., ge=0)
    next_after_sequence: int = Field(..., ge=0)
    has_more: bool
    events: list[AuditEvent]
