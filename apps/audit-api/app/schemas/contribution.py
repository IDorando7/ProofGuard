from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ContributionEligibilityStatus(str, Enum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    PENDING = "pending"


class ContributionComponentName(str, Enum):
    VALIDITY = "validity"
    SEVERITY = "severity"
    REPRODUCIBILITY = "reproducibility"
    UNIQUENESS = "uniqueness"
    QUALITY = "quality"
    PENALTIES = "penalties"


class ContributionSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., min_length=1)
    component: ContributionComponentName
    points: float = Field(..., ge=-100, le=100)
    message: str = Field(..., min_length=1)
    source: str | None = None

    @field_validator("code", "message", mode="before")
    @classmethod
    def trim_required_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class ContributionComponents(BaseModel):
    model_config = ConfigDict(extra="forbid")

    validity: float = Field(default=0, ge=0, le=30)
    severity: float = Field(default=0, ge=0, le=25)
    reproducibility: float = Field(default=0, ge=0, le=25)
    uniqueness: float = Field(default=0, ge=0, le=15)
    quality: float = Field(default=0, ge=0, le=10)
    penalties: float = Field(default=0, ge=-100, le=0)
    raw_total: float = Field(default=0, ge=-100, le=105)


class ContributionScoreRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score_id: str = Field(..., min_length=1)
    scoring_version: str = Field(..., min_length=1)
    submission_id: str = Field(..., min_length=1)
    project_id: str = Field(..., min_length=1)
    finding_id: str = Field(..., min_length=1)
    node_id: str = Field(..., min_length=1)
    validation_id: str | None = Field(default=None, min_length=1)
    reproduction_id: str | None = Field(default=None, min_length=1)
    total_score: float = Field(..., ge=0, le=100)
    eligibility_status: ContributionEligibilityStatus
    eligible_for_reward: bool
    eligibility_reasons: list[str] = Field(..., min_length=1)
    components: ContributionComponents
    signals: list[ContributionSignal]
    reason: str = Field(..., min_length=1)
    created_at: datetime
    updated_at: datetime

    @field_validator(
        "score_id",
        "scoring_version",
        "submission_id",
        "project_id",
        "finding_id",
        "node_id",
        "reason",
        mode="before",
    )
    @classmethod
    def trim_required_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("eligibility_reasons")
    @classmethod
    def validate_eligibility_reasons(cls, value: list[str]) -> list[str]:
        cleaned = [reason.strip() if isinstance(reason, str) else reason for reason in value]
        if any(not isinstance(reason, str) or not reason for reason in cleaned):
            raise ValueError("Eligibility reasons must not be empty")
        return cleaned

    @field_validator("created_at", "updated_at")
    @classmethod
    def require_timezone_aware_datetime(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Contribution timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_reward_eligibility(self) -> "ContributionScoreRecord":
        if self.eligible_for_reward and self.eligibility_status != ContributionEligibilityStatus.ELIGIBLE:
            raise ValueError("Only eligible contributions may be marked eligible for reward")
        return self


class ContributionCalculationRequest(BaseModel):
    """Empty request body used only to reject client-supplied score values."""

    model_config = ConfigDict(extra="forbid")
