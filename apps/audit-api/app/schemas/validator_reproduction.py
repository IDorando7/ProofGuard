from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.reproduction import ReproductionStatus
from app.schemas.poc import ReproductionRunRequest
from app.schemas.validator_attestation import (
    FINAL_REPRODUCTION_STATUSES,
    SHA256_HEX,
    ValidatorAssignmentRole,
    ValidatorReproductionMode,
    ValidatorReproductionRecord,
    _require_aware,
    _validate_identifier,
)


VALIDATOR_REPRODUCTION_EXECUTION_VERSION = "validator_reproduction_execution_v1"
VALIDATOR_SANDBOX_POLICY_VERSION = "week3_sandbox_policy_v1"


class ValidatorReproductionExecutionRequest(BaseModel):
    """Minimal validator-facing request; all execution inputs are server-derived."""

    model_config = ConfigDict(extra="forbid")

    validator_node_id: str
    reproduction_mode: ValidatorReproductionMode = (
        ValidatorReproductionMode.SUBMITTED_POC
    )

    @field_validator("validator_node_id")
    @classmethod
    def validate_node_id(cls, value: str) -> str:
        return _validate_identifier(value)


class IndependentReproductionArtifact(BaseModel):
    """Trusted/internal reference to an artifact already ingested under repo/test."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str
    poc_file: str
    test_name: str

    @field_validator("artifact_id")
    @classmethod
    def validate_artifact_id(cls, value: str) -> str:
        return _validate_identifier(value)

    @model_validator(mode="after")
    def validate_execution_fields(self) -> "IndependentReproductionArtifact":
        ReproductionRunRequest(poc_file=self.poc_file, test_name=self.test_name)
        return self


class ValidatorReproductionJobStatus(str, Enum):
    RUNNING = "running"
    FINALIZED = "finalized"


class ValidatorReproductionJob(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reproduction_request_id: str
    execution_protocol_version: Literal["validator_reproduction_execution_v1"] = (
        VALIDATOR_REPRODUCTION_EXECUTION_VERSION
    )
    validator_assignment_id: str
    validator_committee_id: str
    project_id: str
    routing_id: str
    finding_cluster_id: str
    validator_node_id: str
    validator_operator_id: str
    assignment_role: ValidatorAssignmentRole
    reproduction_mode: ValidatorReproductionMode
    artifact_reference: str
    artifact_fingerprint: str
    source_revision: str
    source_snapshot_fingerprint: str
    environment_fingerprint: str
    request_fingerprint: str
    result_storage_ref: str
    status: ValidatorReproductionJobStatus
    validator_reproduction_id: str | None = None
    created_at: datetime
    finalized_at: datetime | None = None

    @field_validator(
        "reproduction_request_id",
        "validator_assignment_id",
        "validator_committee_id",
        "project_id",
        "routing_id",
        "finding_cluster_id",
        "validator_node_id",
        "validator_operator_id",
        "validator_reproduction_id",
    )
    @classmethod
    def validate_ids(cls, value: str | None) -> str | None:
        return None if value is None else _validate_identifier(value)

    @field_validator("artifact_reference", "source_revision")
    @classmethod
    def validate_logical_references(cls, value: str) -> str:
        return _validate_identifier(value)

    @field_validator("result_storage_ref")
    @classmethod
    def validate_result_storage_ref(cls, value: str) -> str:
        parts = value.split("/")
        if (
            len(parts) != 4
            or parts[0:2] != ["reproductions", "validator"]
            or parts[3] != "reproduction.json"
            or not parts[2].startswith("validator_reproduction_request_")
        ):
            raise ValueError("Invalid validator execution result storage reference")
        _validate_identifier(parts[2])
        return value

    @field_validator(
        "artifact_fingerprint",
        "source_snapshot_fingerprint",
        "environment_fingerprint",
        "request_fingerprint",
    )
    @classmethod
    def validate_fingerprints(cls, value: str) -> str:
        if not SHA256_HEX.fullmatch(value):
            raise ValueError("Execution fingerprints must be lowercase SHA-256")
        return value

    @field_validator("created_at", "finalized_at")
    @classmethod
    def validate_times(cls, value: datetime | None) -> datetime | None:
        return _require_aware(value)

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "ValidatorReproductionJob":
        if self.status == ValidatorReproductionJobStatus.FINALIZED:
            if self.finalized_at is None or self.validator_reproduction_id is None:
                raise ValueError("Finalized execution requires record identity and time")
        elif self.finalized_at is not None or self.validator_reproduction_id is not None:
            raise ValueError("Running execution cannot contain final fields")
        return self


class CommitteeReproductionStageStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    READY = "ready"


class CommitteeReproductionReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    validator_committee_id: str
    project_id: str
    routing_id: str
    finding_cluster_id: str
    authoritative_target: int = Field(..., ge=1)
    authoritative_terminal_count: int = Field(..., ge=0)
    shadow_target: int = Field(..., ge=0)
    shadow_terminal_count: int = Field(..., ge=0)
    pending_authoritative_assignment_ids: list[str]
    pending_shadow_assignment_ids: list[str]
    terminal_status_counts: dict[ReproductionStatus, int]
    shadow_terminal_status_counts: dict[ReproductionStatus, int]
    status: CommitteeReproductionStageStatus
    ready_for_attestation_or_consensus_stage: bool

    @model_validator(mode="after")
    def validate_readiness(self) -> "CommitteeReproductionReadiness":
        ready = self.authoritative_terminal_count == self.authoritative_target
        if self.ready_for_attestation_or_consensus_stage != ready:
            raise ValueError("Readiness must depend only on authoritative completion")
        if (self.status == CommitteeReproductionStageStatus.READY) != ready:
            raise ValueError("Ready stage status is inconsistent")
        return self


class ValidatorReproductionListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int = Field(..., ge=0)
    reproductions: list[ValidatorReproductionRecord]

    @model_validator(mode="after")
    def validate_total(self) -> "ValidatorReproductionListResponse":
        if self.total != len(self.reproductions):
            raise ValueError("Reproduction total must match records")
        return self


def is_terminal_reproduction_status(status: ReproductionStatus) -> bool:
    return status in FINAL_REPRODUCTION_STATUSES
