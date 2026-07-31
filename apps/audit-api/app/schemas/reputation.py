from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.contribution import ContributionEligibilityStatus
from app.schemas.node import NodeStatistics, normalize_category


class ReputationEventType(str, Enum):
    ACCEPTED_CONTRIBUTION = "accepted_contribution"
    DUPLICATE_FINDING = "duplicate_finding"
    REJECTED_FINDING = "rejected_finding"
    OUT_OF_SCOPE_FINDING = "out_of_scope_finding"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    UNSAFE_SUBMISSION = "unsafe_submission"
    UNSUPPORTED_SUBMISSION = "unsupported_submission"


class ReputationEventApplicationStatus(str, Enum):
    PREPARED = "prepared"
    APPLIED = "applied"


class ReputationProcessingStatus(str, Enum):
    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"
    PENDING = "pending"


class ReputationDeltaComponents(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_delta: float = Field(default=0, ge=-1, le=1)
    contribution_bonus: float = Field(default=0, ge=0, le=1)
    severity_bonus: float = Field(default=0, ge=0, le=1)
    penalty: float = Field(default=0, ge=-1, le=0)
    total_delta: float = Field(default=0, ge=-1, le=1)

    @model_validator(mode="after")
    def validate_total_delta(self) -> "ReputationDeltaComponents":
        expected = round(
            self.base_delta + self.contribution_bonus + self.severity_bonus + self.penalty,
            6,
        )
        if round(self.total_delta, 6) != expected:
            raise ValueError("total_delta must equal the sum of its reputation components")
        return self


class ReputationSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., min_length=1)
    delta: float = Field(..., ge=-1, le=1)
    message: str = Field(..., min_length=1)
    source: str | None = None

    @field_validator("code", "message", mode="before")
    @classmethod
    def trim_required_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class NodeStatisticsDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_submissions: int = Field(default=0, ge=0, le=1)
    accepted_submissions: int = Field(default=0, ge=0, le=1)
    rejected_submissions: int = Field(default=0, ge=0, le=1)
    duplicate_submissions: int = Field(default=0, ge=0, le=1)
    out_of_scope_submissions: int = Field(default=0, ge=0, le=1)
    unsafe_submissions: int = Field(default=0, ge=0, le=1)


class ReputationEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(..., min_length=1)
    reputation_version: str = Field(..., min_length=1)
    application_status: ReputationEventApplicationStatus
    node_id: str = Field(..., min_length=1)
    submission_id: str = Field(..., min_length=1)
    project_id: str = Field(..., min_length=1)
    finding_id: str = Field(..., min_length=1)
    category: str
    validation_id: str | None = Field(default=None, min_length=1)
    reproduction_id: str | None = Field(default=None, min_length=1)
    contribution_score_id: str = Field(..., min_length=1)
    validation_status: str = Field(..., min_length=1)
    reproduction_status: str | None = Field(default=None, min_length=1)
    contribution_score: float = Field(..., ge=0, le=100)
    contribution_eligibility_status: str = Field(..., min_length=1)
    normalized_severity: str = Field(..., min_length=1)
    event_type: ReputationEventType
    previous_reputation: float = Field(..., ge=0, le=1)
    delta_components: ReputationDeltaComponents
    new_reputation: float = Field(..., ge=0, le=1)
    statistics_delta: NodeStatisticsDelta
    previous_statistics: dict[str, int]
    new_statistics: dict[str, int]
    signals: list[ReputationSignal]
    reason: str = Field(..., min_length=1)
    source_fingerprint: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
    applied_at: datetime | None = None
    updated_at: datetime

    @field_validator(
        "event_id",
        "reputation_version",
        "node_id",
        "submission_id",
        "project_id",
        "finding_id",
        "contribution_score_id",
        "validation_status",
        "contribution_eligibility_status",
        "normalized_severity",
        "reason",
        mode="before",
    )
    @classmethod
    def trim_required_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("category", mode="before")
    @classmethod
    def normalize_event_category(cls, value: Any) -> str:
        return normalize_category(value)

    @field_validator("contribution_eligibility_status", mode="before")
    @classmethod
    def validate_contribution_eligibility(cls, value: Any) -> str:
        raw = value.value if isinstance(value, ContributionEligibilityStatus) else value
        try:
            return ContributionEligibilityStatus(raw).value
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid contribution eligibility status") from exc

    @field_validator("previous_statistics", "new_statistics")
    @classmethod
    def validate_statistics_snapshot(cls, value: dict[str, int]) -> dict[str, int]:
        validated = NodeStatistics.model_validate(value)
        return validated.model_dump()

    @field_validator("created_at", "applied_at", "updated_at")
    @classmethod
    def require_timezone_aware_datetime(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Reputation timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_application_timestamp(self) -> "ReputationEvent":
        if self.application_status == ReputationEventApplicationStatus.APPLIED and self.applied_at is None:
            raise ValueError("Applied reputation events require applied_at")
        return self


class NodeReputationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(..., min_length=1)
    current_reputation: float = Field(..., ge=0, le=1)
    statistics: NodeStatistics
    total_reputation_events: int = Field(..., ge=0)
    last_event_at: datetime | None
    reputation_version: str = Field(..., min_length=1)


class ReputationProcessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    processing_status: ReputationProcessingStatus
    submission_id: str = Field(..., min_length=1)
    node_id: str = Field(..., min_length=1)
    event_id: str | None = Field(default=None, min_length=1)
    previous_reputation: float = Field(..., ge=0, le=1)
    reputation_delta: float = Field(..., ge=-1, le=1)
    current_reputation: float = Field(..., ge=0, le=1)
    message: str = Field(..., min_length=1)
    event: ReputationEvent | None = None

    @model_validator(mode="after")
    def validate_pending_response(self) -> "ReputationProcessResponse":
        if self.processing_status == ReputationProcessingStatus.PENDING:
            if self.event_id is not None or self.event is not None or self.reputation_delta != 0:
                raise ValueError("Pending reputation processing cannot include an event or delta")
        return self


class ReputationProcessRequest(BaseModel):
    """Empty body model that rejects client-controlled reputation values."""

    model_config = ConfigDict(extra="forbid")
