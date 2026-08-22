from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.node import normalize_category


class RewardDomain(str, Enum):
    """Reward accounting domains with intentionally independent semantics."""

    CLIENT_TASK = "client_task"
    NETWORK_PROTOCOL = "network_protocol"


class RewardUnit(str, Enum):
    PROTOCOL_POINTS = "protocol_points"


class RewardPoolKind(str, Enum):
    """Independent, single-consumption streams within one task budget."""

    MINER = "miner"
    VALIDATOR = "validator"
    PROTOCOL = "protocol"


class RewardCycleStatus(str, Enum):
    DRAFT = "draft"
    CALCULATED = "calculated"
    FINALIZED = "finalized"


class RewardEventStatus(str, Enum):
    CALCULATED = "calculated"
    FINALIZED = "finalized"


class RewardEligibilityReasonCode(str, Enum):
    ELIGIBLE = "eligible"
    CONTRIBUTION_MISSING = "contribution_missing"
    CONTRIBUTION_PENDING = "contribution_pending"
    CONTRIBUTION_INELIGIBLE = "contribution_ineligible"
    VALIDATION_NOT_ACCEPTED = "validation_not_accepted"
    REPRODUCTION_NOT_CONFIRMED = "reproduction_not_confirmed"
    DUPLICATE = "duplicate"
    OUT_OF_SCOPE = "out_of_scope"
    REJECTED = "rejected"
    UNSAFE = "unsafe"
    UNSUPPORTED = "unsupported"
    ALREADY_REWARDED = "already_rewarded"
    ALREADY_PENALIZED = "already_penalized"
    SOURCE_MISMATCH = "source_mismatch"


class RewardWeightComponents(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contribution_score: float = Field(..., ge=0, le=100)
    contribution_weight: float = Field(..., ge=0, le=100)
    reputation_score: float = Field(..., ge=0, le=1)
    reputation_multiplier: float = Field(..., ge=0, le=2)
    category_multiplier: float = Field(..., ge=0, le=2)
    raw_weight: float = Field(..., ge=0)

    @model_validator(mode="after")
    def validate_formula(self) -> "RewardWeightComponents":
        if round(self.contribution_weight, 6) != round(self.contribution_score, 6):
            raise ValueError("contribution_weight must equal contribution_score")
        expected = round(
            self.contribution_weight * self.reputation_multiplier * self.category_multiplier,
            6,
        )
        if round(self.raw_weight, 6) != expected:
            raise ValueError("raw_weight must equal contribution_weight multiplied by both multipliers")
        return self


class RewardEligibilityResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    submission_id: str = Field(..., min_length=1)
    node_id: str = Field(..., min_length=1)
    eligible: bool
    reason_code: RewardEligibilityReasonCode
    reasons: list[str] = Field(..., min_length=1)
    contribution_score_id: str | None = Field(default=None, min_length=1)
    reputation_event_id: str | None = Field(default=None, min_length=1)
    contribution_score: float | None = Field(default=None, ge=0, le=100)
    reputation_score: float | None = Field(default=None, ge=0, le=1)

    @field_validator("reasons")
    @classmethod
    def validate_reasons(cls, value: list[str]) -> list[str]:
        cleaned = [reason.strip() if isinstance(reason, str) else reason for reason in value]
        if any(not isinstance(reason, str) or not reason for reason in cleaned):
            raise ValueError("Eligibility reasons must not be empty")
        return cleaned

    @model_validator(mode="after")
    def validate_reason_code(self) -> "RewardEligibilityResult":
        if self.eligible != (self.reason_code == RewardEligibilityReasonCode.ELIGIBLE):
            raise ValueError("Only the eligible reason code may produce an eligible result")
        return self


class RewardAllocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    submission_id: str = Field(..., min_length=1)
    node_id: str = Field(..., min_length=1)
    project_id: str = Field(..., min_length=1)
    finding_id: str = Field(..., min_length=1)
    category: str
    eligible: bool
    eligibility_reason_code: RewardEligibilityReasonCode
    eligibility_reasons: list[str] = Field(..., min_length=1)
    weight_components: RewardWeightComponents | None = None
    allocation_ratio: float = Field(..., ge=0, le=1)
    reward_amount: float = Field(..., ge=0)
    reward_unit: RewardUnit = RewardUnit.PROTOCOL_POINTS

    @field_validator("category", mode="before")
    @classmethod
    def normalize_allocation_category(cls, value: Any) -> str:
        return normalize_category(value)

    @field_validator("eligibility_reasons")
    @classmethod
    def validate_reasons(cls, value: list[str]) -> list[str]:
        if any(not isinstance(reason, str) or not reason.strip() for reason in value):
            raise ValueError("Eligibility reasons must not be empty")
        return [reason.strip() for reason in value]

    @model_validator(mode="after")
    def validate_eligibility_shape(self) -> "RewardAllocation":
        if not self.eligible:
            if self.allocation_ratio != 0 or self.reward_amount != 0:
                raise ValueError("Ineligible allocations must have zero ratio and reward")
        elif self.weight_components is None:
            raise ValueError("Eligible allocations require weight components")
        if self.eligible != (self.eligibility_reason_code == RewardEligibilityReasonCode.ELIGIBLE):
            raise ValueError("Allocation eligibility and reason code do not match")
        return self


class _RewardCycleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reward_pool: float = Field(default=1000.0, gt=0, le=1_000_000)
    submission_ids: list[str] | None = None
    description: str | None = Field(default=None, max_length=500)

    @field_validator("reward_pool")
    @classmethod
    def normalize_pool_precision(cls, value: float) -> float:
        return round(value, 6)

    @field_validator("submission_ids")
    @classmethod
    def validate_unique_submission_ids(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = [item.strip() if isinstance(item, str) else item for item in value]
        if any(not isinstance(item, str) or not item for item in cleaned):
            raise ValueError("Submission identifiers must not be empty")
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("Submission identifiers must be unique")
        return cleaned

    @field_validator("description", mode="before")
    @classmethod
    def trim_description(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class RewardCycleRequest(_RewardCycleInput):
    """Public request body; project_id is always taken from the URL."""


class RewardCycleActionRequest(BaseModel):
    """Empty body that rejects client-controlled calculation or finalization inputs."""

    model_config = ConfigDict(extra="forbid")


class RewardCycleCreate(_RewardCycleInput):
    project_id: str = Field(..., min_length=1)

    @field_validator("project_id", mode="before")
    @classmethod
    def trim_project_id(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class RewardCycle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cycle_id: str = Field(..., min_length=1)
    reward_version: str = Field(..., min_length=1)
    project_id: str = Field(..., min_length=1)
    reward_pool: float = Field(..., gt=0, le=1_000_000)
    reward_unit: RewardUnit
    status: RewardCycleStatus
    submission_ids: list[str]
    allocations: list[RewardAllocation]
    eligible_submissions: int = Field(..., ge=0)
    ineligible_submissions: int = Field(..., ge=0)
    total_raw_weight: float = Field(..., ge=0)
    total_allocated: float = Field(..., ge=0)
    undistributed_amount: float = Field(..., ge=0)
    source_fingerprint: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    description: str | None = Field(default=None, max_length=500)
    created_at: datetime
    calculated_at: datetime | None = None
    finalized_at: datetime | None = None
    updated_at: datetime

    @field_validator("created_at", "calculated_at", "finalized_at", "updated_at")
    @classmethod
    def require_timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Reward timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_cycle_totals_and_timestamps(self) -> "RewardCycle":
        if round(self.total_allocated, 6) > round(self.reward_pool, 6):
            raise ValueError("total_allocated cannot exceed reward_pool")
        if self.status in {RewardCycleStatus.CALCULATED, RewardCycleStatus.FINALIZED} and self.calculated_at is None:
            raise ValueError("Calculated and finalized cycles require calculated_at")
        if self.status == RewardCycleStatus.FINALIZED and self.finalized_at is None:
            raise ValueError("Finalized cycles require finalized_at")
        return self


class RewardEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reward_event_id: str = Field(..., min_length=1)
    reward_version: str = Field(..., min_length=1)
    event_status: RewardEventStatus
    cycle_id: str = Field(..., min_length=1)
    project_id: str = Field(..., min_length=1)
    submission_id: str = Field(..., min_length=1)
    node_id: str = Field(..., min_length=1)
    finding_id: str = Field(..., min_length=1)
    category: str
    contribution_score_id: str = Field(..., min_length=1)
    reputation_event_id: str | None = Field(default=None, min_length=1)
    contribution_score: float = Field(..., ge=0, le=100)
    reputation_score: float = Field(..., ge=0, le=1)
    reputation_multiplier: float = Field(..., ge=0, le=2)
    category_multiplier: float = Field(..., ge=0, le=2)
    raw_weight: float = Field(..., ge=0)
    allocation_ratio: float = Field(..., ge=0, le=1)
    reward_amount: float = Field(..., ge=0)
    reward_unit: RewardUnit
    source_fingerprint: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(..., min_length=1)
    created_at: datetime
    finalized_at: datetime | None = None
    updated_at: datetime

    @field_validator("category", mode="before")
    @classmethod
    def normalize_event_category(cls, value: Any) -> str:
        return normalize_category(value)

    @field_validator("created_at", "finalized_at", "updated_at")
    @classmethod
    def require_timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("RewardEvent timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_finalization(self) -> "RewardEvent":
        if self.event_status == RewardEventStatus.FINALIZED and self.finalized_at is None:
            raise ValueError("Finalized reward events require finalized_at")
        return self


class RewardCycleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cycle: RewardCycle
    message: str = Field(..., min_length=1)


class NodeRewardSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str = Field(..., min_length=1)
    total_reward_events: int = Field(..., ge=0)
    total_protocol_points: float = Field(..., ge=0)
    projects_rewarded: int = Field(..., ge=0)
    last_reward_at: datetime | None
    rewards: list[RewardEvent] = Field(default_factory=list)


class PenaltyReasonCode(str, Enum):
    UNSAFE_SUBMISSION = "unsafe_submission"
    MALICIOUS_PAYLOAD = "malicious_payload"
    DUPLICATE_SPAM = "duplicate_spam"
    REPEATED_FALSE_POSITIVE = "repeated_false_positive"
    MANUAL_REVIEW_REQUIRED = "manual_review_required"


class PenaltyEventStatus(str, Enum):
    RECORDED = "recorded"


class PenaltyEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    penalty_event_id: str = Field(..., min_length=1)
    penalty_version: str = Field(..., min_length=1)
    status: PenaltyEventStatus
    node_id: str = Field(..., min_length=1)
    submission_id: str = Field(..., min_length=1)
    project_id: str = Field(..., min_length=1)
    finding_id: str = Field(..., min_length=1)
    category: str
    reason_code: PenaltyReasonCode
    reward_denied: bool
    protocol_penalty_points: float = Field(..., ge=0)
    reputation_event_id: str | None = Field(default=None, min_length=1)
    simulated_stake_loss: float = Field(..., ge=0)
    executed_onchain: bool
    requires_human_review: bool
    source_fingerprint: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(..., min_length=1)
    created_at: datetime
    updated_at: datetime

    @field_validator("category", mode="before")
    @classmethod
    def normalize_penalty_category(cls, value: Any) -> str:
        return normalize_category(value)

    @field_validator("created_at", "updated_at")
    @classmethod
    def require_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("PenaltyEvent timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def enforce_offchain_v0(self) -> "PenaltyEvent":
        if self.executed_onchain:
            raise ValueError("Day 6 penalty events cannot be executed on-chain")
        if self.simulated_stake_loss != 0:
            raise ValueError("Day 6 penalty events cannot apply stake loss")
        if self.reason_code == PenaltyReasonCode.UNSAFE_SUBMISSION:
            if not self.reward_denied or self.protocol_penalty_points != 25:
                raise ValueError("Unsafe Day 6 penalties must deny reward and assign 25 penalty points")
            if not self.requires_human_review:
                raise ValueError("Unsafe Day 6 penalties require human review")
        return self


class PenaltyProcessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    submission_id: str = Field(..., min_length=1)
    node_id: str = Field(..., min_length=1)
    created: bool
    event: PenaltyEvent
    message: str = Field(..., min_length=1)


class PenaltyProcessRequest(BaseModel):
    """Empty body that rejects client-controlled penalty values."""

    model_config = ConfigDict(extra="forbid")
