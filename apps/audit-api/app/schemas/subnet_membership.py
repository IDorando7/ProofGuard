import re
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.category_score import CategoryScoreBand
from app.schemas.finding import FindingCategory
from app.schemas.node import NodeStatus, normalize_category
from app.schemas.subnet import (
    SAFE_NODE_ID,
    SAFE_SUBNET_ID,
    SubnetMemberRecord,
    SubnetMemberStatus,
    SubnetStatus,
)


MEMBERSHIP_VERSION = "subnet_membership_v0"
MEMBERSHIP_POLICY_VERSION = "membership_policy_v0"


class MembershipReasonCode(str, Enum):
    ELIGIBLE_CANDIDATE = "eligible_candidate"
    ELIGIBLE_PROBATION = "eligible_probation"
    ELIGIBLE_ACTIVE = "eligible_active"
    ELIGIBLE_EXPERT = "eligible_expert"

    INSUFFICIENT_HISTORY = "insufficient_history"
    INSUFFICIENT_SCORE = "insufficient_score"
    INSUFFICIENT_ACCEPTED_FINDINGS = "insufficient_accepted_findings"

    NODE_INACTIVE = "node_inactive"
    NODE_SUSPENDED = "node_suspended"
    NODE_BANNED = "node_banned"

    SUBNET_INACTIVE = "subnet_inactive"
    SUBNET_SUSPENDED = "subnet_suspended"
    SUBNET_ARCHIVED = "subnet_archived"

    CATEGORY_NOT_SUPPORTED = "category_not_supported"
    RECENT_UNSAFE_SUBMISSION = "recent_unsafe_submission"
    HISTORICAL_UNSAFE_PREVENTS_EXPERT = "historical_unsafe_prevents_expert"

    ACTIVE_CAPACITY_REACHED = "active_capacity_reached"
    HYSTERESIS_PRESERVED_ACTIVE = "hysteresis_preserved_active"
    HYSTERESIS_PRESERVED_EXPERT = "hysteresis_preserved_expert"

    ADMINISTRATIVELY_SUSPENDED = "administratively_suspended"
    ADMINISTRATIVELY_REMOVED = "administratively_removed"


class MembershipDecisionStatus(str, Enum):
    CREATED = "created"
    CHANGED = "changed"
    UNCHANGED = "unchanged"


class MembershipSourceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_status: NodeStatus
    node_supported_categories: list[FindingCategory]
    subnet_status: SubnetStatus
    subnet_minimum_category_score: float = Field(..., ge=0.0, le=1.0)
    subnet_minimum_finalized_submissions: int = Field(..., ge=0)
    subnet_maximum_active_nodes: int = Field(..., ge=1)
    category_score_id: str = Field(..., min_length=1)
    category_score: float = Field(..., ge=0.0, le=1.0)
    score_band: CategoryScoreBand
    experience_confidence: float = Field(..., ge=0.0, le=1.0)
    performance_id: str = Field(..., min_length=1)
    finalized_submissions: int = Field(..., ge=0)
    accepted_unique_submissions: int = Field(..., ge=0)
    unsafe_submissions: int = Field(..., ge=0)
    recent_unsafe_event_ids: list[str]
    previous_membership_status: SubnetMemberStatus | None = None

    @field_validator("node_supported_categories", mode="before")
    @classmethod
    def normalize_categories(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return value
        normalized = [normalize_category(item) for item in value]
        if len(normalized) != len(set(normalized)):
            raise ValueError("Supported categories must be unique")
        return sorted(normalized)

    @field_validator("recent_unsafe_event_ids")
    @classmethod
    def validate_event_ids(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("Unsafe event identifiers must not be empty")
        if len(value) != len(set(value)):
            raise ValueError("Unsafe event identifiers must be unique")
        if value != sorted(value):
            raise ValueError("Unsafe event identifiers must be deterministically sorted")
        return value


class SubnetMembershipDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_id: str = Field(..., min_length=1, max_length=256)
    policy_version: str = Field(..., min_length=1, max_length=64)
    subnet_id: str = Field(..., min_length=1, max_length=128)
    node_id: str = Field(..., min_length=1, max_length=128)
    category: FindingCategory
    previous_status: SubnetMemberStatus | None = None
    recommended_status: SubnetMemberStatus
    final_status: SubnetMemberStatus
    reason_codes: list[MembershipReasonCode] = Field(..., min_length=1)
    reasons: list[str] = Field(..., min_length=1)
    source_snapshot: MembershipSourceSnapshot
    source_fingerprint: str
    capacity_adjusted: bool
    created_at: datetime

    @field_validator("category", mode="before")
    @classmethod
    def normalize_decision_category(cls, value: Any) -> str:
        return normalize_category(value)

    @field_validator("subnet_id")
    @classmethod
    def validate_subnet_id(cls, value: str) -> str:
        if not SAFE_SUBNET_ID.fullmatch(value):
            raise ValueError("Invalid subnet identifier")
        return value

    @field_validator("node_id")
    @classmethod
    def validate_node_id(cls, value: str) -> str:
        if not SAFE_NODE_ID.fullmatch(value) or value in {".", ".."}:
            raise ValueError("Invalid node identifier")
        return value

    @field_validator("source_fingerprint")
    @classmethod
    def validate_fingerprint(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("Source fingerprint must be a lowercase SHA-256 digest")
        return value

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(
        cls, value: list[MembershipReasonCode]
    ) -> list[MembershipReasonCode]:
        if len(value) != len(set(value)):
            raise ValueError("Membership reason codes must be unique")
        return value

    @field_validator("reasons")
    @classmethod
    def validate_reasons(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(not item for item in cleaned):
            raise ValueError("Membership reasons must not be empty")
        return cleaned

    @field_validator("created_at")
    @classmethod
    def require_timezone_aware_datetime(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Membership decision timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_identity_and_capacity(self) -> "SubnetMembershipDecision":
        if self.subnet_id != f"subnet_{self.category.value}":
            raise ValueError("Decision category must match the subnet category")
        if self.source_snapshot.previous_membership_status != self.previous_status:
            raise ValueError("Snapshot previous status must match the decision")
        if self.capacity_adjusted and (
            self.recommended_status
            not in {SubnetMemberStatus.ACTIVE, SubnetMemberStatus.EXPERT}
            or self.final_status != SubnetMemberStatus.PROBATION
        ):
            raise ValueError("Capacity adjustment must downgrade a core recommendation")
        return self


class MembershipEvaluationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subnet_id: str
    node_id: str
    status: MembershipDecisionStatus
    previous_member: SubnetMemberRecord | None
    current_member: SubnetMemberRecord
    decision: SubnetMembershipDecision


class SubnetMembershipRefreshResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subnet_id: str
    category: FindingCategory
    evaluated_nodes: int = Field(..., ge=0)
    created_members: int = Field(..., ge=0)
    changed_members: int = Field(..., ge=0)
    unchanged_members: int = Field(..., ge=0)
    candidate_members: int = Field(..., ge=0)
    probation_members: int = Field(..., ge=0)
    active_members: int = Field(..., ge=0)
    expert_members: int = Field(..., ge=0)
    suspended_members: int = Field(..., ge=0)
    removed_members: int = Field(..., ge=0)
    results: list[MembershipEvaluationResult]
    errors: list[str]
    refreshed_at: datetime

    @field_validator("refreshed_at")
    @classmethod
    def require_timezone_aware_datetime(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Refresh timestamps must be timezone-aware")
        return value


class MembershipAdministrativeActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(..., min_length=3, max_length=500)

    @field_validator("reason", mode="before")
    @classmethod
    def trim_reason(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class MembershipRefreshRequest(BaseModel):
    """Empty body that rejects client-controlled membership values."""

    model_config = ConfigDict(extra="forbid")
