from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.finding import FindingCategory, FindingCreate
from app.schemas.node import normalize_category
from app.utils.protocol_serialization import protocol_fingerprint


AGENT_EXECUTION_VERSION = "agent_execution_v1"
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,255}$")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


class AgentExecutorType(str, Enum):
    LOCAL = "local"
    REMOTE = "remote"


class AgentExecutionStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class AgentExecutionRecord(BaseModel):
    """Durable one-per-assignment execution identity for local or future workers."""

    model_config = ConfigDict(extra="forbid")

    agent_execution_id: str = Field(..., min_length=1, max_length=256)
    agent_execution_version: Literal["agent_execution_v1"] = AGENT_EXECUTION_VERSION
    audit_run_id: str = Field(..., min_length=1, max_length=256)
    project_id: str = Field(..., min_length=1, max_length=256)
    routing_id: str = Field(..., min_length=1, max_length=256)
    routing_assignment_id: str = Field(..., min_length=1, max_length=256)
    node_id: str = Field(..., min_length=1, max_length=128)
    operator_id: str = Field(..., min_length=1, max_length=128)
    category: FindingCategory
    executor_type: AgentExecutorType
    agent_type: str = Field(..., min_length=1, max_length=128)
    agent_version: str = Field(..., min_length=1, max_length=64)
    status: AgentExecutionStatus
    attempt_count: int = Field(default=0, ge=0)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    finding_ids: list[str] = Field(default_factory=list)
    submission_ids: list[str] = Field(default_factory=list)
    finding_count: int = Field(default=0, ge=0)
    submission_count: int = Field(default=0, ge=0)
    error_code: str | None = Field(default=None, min_length=1, max_length=128)
    error_message: str | None = Field(default=None, min_length=1, max_length=1000)
    source_fingerprint: str
    created_at: datetime
    updated_at: datetime

    @field_validator(
        "agent_execution_id",
        "audit_run_id",
        "project_id",
        "routing_id",
        "routing_assignment_id",
        "node_id",
        "operator_id",
        "agent_type",
        "agent_version",
        "error_code",
    )
    @classmethod
    def validate_identifiers(cls, value: str | None) -> str | None:
        if value is not None and not SAFE_ID.fullmatch(value):
            raise ValueError("Agent execution identifiers must be protocol-safe")
        return value

    @field_validator("category", mode="before")
    @classmethod
    def normalize_execution_category(cls, value: Any) -> str:
        return normalize_category(value)

    @field_validator("source_fingerprint")
    @classmethod
    def validate_fingerprint(cls, value: str) -> str:
        if not SHA256_HEX.fullmatch(value):
            raise ValueError("source_fingerprint must be a lowercase SHA-256 digest")
        return value

    @field_validator("started_at", "completed_at", "created_at", "updated_at")
    @classmethod
    def validate_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Agent execution timestamps must be timezone-aware")
        return value

    @field_validator("error_message")
    @classmethod
    def validate_safe_error(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("error_message must not be empty")
        lowered = cleaned.lower()
        if lowered.startswith(("/", "~/")) or "/home/" in lowered or "/tmp/" in lowered:
            raise ValueError("error_message must not expose host paths")
        return cleaned

    @field_validator("finding_ids", "submission_ids")
    @classmethod
    def validate_sorted_refs(cls, value: list[str]) -> list[str]:
        if any(not SAFE_ID.fullmatch(item) for item in value):
            raise ValueError("Execution references must be protocol-safe")
        if value != sorted(set(value)):
            raise ValueError("Execution references must be unique and sorted")
        return value

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "AgentExecutionRecord":
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        if self.finding_count != len(self.finding_ids):
            raise ValueError("finding_count must match finding_ids")
        if self.submission_count != len(self.submission_ids):
            raise ValueError("submission_count must match submission_ids")
        if self.status == AgentExecutionStatus.PENDING:
            if self.started_at is not None or self.completed_at is not None:
                raise ValueError("Pending execution cannot have execution timestamps")
            if self.attempt_count != 0:
                raise ValueError("Pending execution cannot have attempts")
        elif self.status == AgentExecutionStatus.RUNNING:
            if self.started_at is None or self.completed_at is not None:
                raise ValueError("Running execution requires started_at only")
            if self.attempt_count < 1:
                raise ValueError("Running execution requires an attempt")
        elif self.status == AgentExecutionStatus.COMPLETED:
            if self.started_at is None or self.completed_at is None:
                raise ValueError("Completed execution requires timestamps")
            if self.error_code is not None or self.error_message is not None:
                raise ValueError("Completed execution cannot expose an error")
        elif self.status == AgentExecutionStatus.FAILED:
            if (
                self.started_at is None
                or self.completed_at is None
                or self.error_code is None
                or self.error_message is None
            ):
                raise ValueError("Failed execution requires timestamps and safe error details")
            if self.finding_ids or self.submission_ids:
                raise ValueError("Failed execution cannot contain partial ingestion")
        if self.completed_at is not None:
            if self.completed_at < self.started_at:  # type: ignore[operator]
                raise ValueError("completed_at cannot precede started_at")
            if self.duration_ms is None:
                raise ValueError("Terminal execution requires duration_ms")
        elif self.duration_ms is not None:
            raise ValueError("Non-terminal execution cannot have duration_ms")
        return self


class AgentExecutionResult(BaseModel):
    """Normalized executor boundary; concrete agent objects never escape it."""

    model_config = ConfigDict(extra="forbid")

    agent_execution_id: str
    success: bool
    finding_candidates: list[FindingCreate] = Field(default_factory=list)
    summary: str = Field(..., min_length=1, max_length=1000)
    metadata: dict[str, str | int | bool | None] = Field(default_factory=dict)
    error_code: str | None = Field(default=None, max_length=128)
    error_message: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def validate_result(self) -> "AgentExecutionResult":
        if not SAFE_ID.fullmatch(self.agent_execution_id):
            raise ValueError("Invalid agent_execution_id")
        if self.success and (self.error_code is not None or self.error_message is not None):
            raise ValueError("Successful result cannot contain an error")
        if not self.success and (self.error_code is None or self.error_message is None):
            raise ValueError("Failed result requires safe error information")
        if not self.success and self.finding_candidates:
            raise ValueError("Failed result cannot contain partial findings")
        return self


class AgentExecutionListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audit_run_id: str
    total: int = Field(..., ge=0)
    agent_executions: list[AgentExecutionRecord]


def agent_execution_source_fingerprint(payload: dict[str, Any]) -> str:
    return protocol_fingerprint(
        {"agent_execution_version": AGENT_EXECUTION_VERSION, **payload}
    )
