from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.finding import FindingCategory, FindingSeverity
from app.schemas.finding_cluster import FINDING_CLUSTER_POLICY_VERSION
from app.schemas.node import normalize_category
from app.schemas.report_quality import ReportQualityConfig
from app.schemas.reward import (
    RewardCycleStatus,
    RewardDomain,
    RewardEventStatus,
    RewardUnit,
)
from app.schemas.subnet_reward import MAX_PROTOCOL_POINTS, PROTOCOL_POINTS_QUANTUM
from app.schemas.task_finding_reward import FindingAllocationScope, TaskFindingRewardConfig
from app.schemas.task_operator_reward import (
    QUALITY_WEIGHT_QUANTUM,
    TaskOperatorRewardConfig,
)
from app.schemas.task_reward import TASK_REWARD_POLICY_VERSION, TaskRewardPoolConfig


WEEK7_REWARD_CYCLE_VERSION = "week7_task_reward_cycle_v1"
WEEK7_REWARD_POLICY_VERSION = "scalable_multi_agent_reward_v1"
WEEK7_REWARD_CONFIGURATION_VERSION = "week7_reward_cycle_config_v1"
WEEK7_REWARD_EVENT_VERSION = "week7_task_reward_event_v1"
WEEK7_REWARD_FINGERPRINT_VERSION = "reward_cycle_fingerprint_v1"
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,255}$")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
CYCLE_ID = re.compile(r"^week7_task_reward_cycle_[0-9a-f]{32}$")
EVENT_ID = re.compile(r"^week7_task_reward_event_[0-9a-f]{64}$")


class Week7RewardProcessingStatus(str, Enum):
    CREATED = "created"
    UNCHANGED = "unchanged"
    CALCULATED = "calculated"
    RECALCULATED = "recalculated"
    FINALIZED = "finalized"
    ALREADY_FINALIZED = "already_finalized"


class Week7RewardHistorySubject(str, Enum):
    OPERATOR = "operator"
    NODE = "node"
    SUBMISSION = "submission"
    FINDING_CLUSTER = "finding_cluster"


class Week7RewardVerificationStatus(str, Enum):
    DRAFT = "draft"
    CALCULATED_NOT_FINALIZED = "calculated_not_finalized"
    STALE_CALCULATION = "stale_calculation"
    PARTIAL_EVENT_PUBLICATION = "partial_event_publication"
    EVENT_SET_COMPLETE_STATUS_NOT_FINALIZED = (
        "event_set_complete_status_not_finalized"
    )
    CLEAN_FINALIZED = "clean_finalized"
    FINALIZED_EVENT_MISMATCH = "finalized_event_mismatch"
    CORRUPT_REFERENCES = "corrupt_references"


def _identifier(value: str, label: str) -> str:
    cleaned = value.strip()
    if (
        not SAFE_IDENTIFIER.fullmatch(cleaned)
        or cleaned in {".", ".."}
        or "/" in cleaned
        or "\\" in cleaned
    ):
        raise ValueError(f"Invalid {label} identifier")
    return cleaned


def _decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, float):
        raise ValueError(f"{label} must use Decimal or a decimal string")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a valid decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{label} must be finite")
    return result


def _points(value: Any, label: str, *, positive: bool = False) -> Decimal:
    result = _decimal(value, label)
    if result < 0 or (positive and result <= 0) or result > MAX_PROTOCOL_POINTS:
        raise ValueError(f"{label} is outside the protocol-points range")
    if result != result.quantize(PROTOCOL_POINTS_QUANTUM):
        raise ValueError(f"{label} must use at most six decimal places")
    return result


def _ratio(value: Any, label: str) -> Decimal:
    result = _decimal(value, label)
    if result < 0 or result > 1:
        raise ValueError(f"{label} must be between zero and one")
    if result != result.quantize(PROTOCOL_POINTS_QUANTUM):
        raise ValueError(f"{label} must use at most six decimal places")
    return result


def _fingerprint(value: str | None, label: str) -> str | None:
    if value is not None and not SHA256_HEX.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256 fingerprint")
    return value


def _aware(value: datetime | None, label: str) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError(f"{label} must be timezone-aware")
    return value


class Week7TaskRewardCycleCreateRequest(BaseModel):
    """Administrative cycle request; all economic outputs remain server-derived."""

    model_config = ConfigDict(extra="forbid")

    task_reward_budget_id: str
    allocation_scope: FindingAllocationScope | None = None
    category_weights: dict[FindingCategory, Decimal] | None = None
    description: str | None = Field(default=None, max_length=500)

    @field_validator("task_reward_budget_id")
    @classmethod
    def validate_budget_id(cls, value: str) -> str:
        return _identifier(value, "task reward budget")

    @field_validator("category_weights", mode="before")
    @classmethod
    def normalize_weights(cls, value: Any) -> Any:
        if value is None or not isinstance(value, dict):
            return value
        normalized: dict[str, Any] = {}
        for category, weight in value.items():
            key = normalize_category(category)
            if key in normalized:
                raise ValueError("Category weights must be unique")
            if isinstance(weight, float):
                raise ValueError("Category weights must use Decimal strings")
            normalized[key] = weight
        return normalized

    @field_validator("category_weights")
    @classmethod
    def validate_weights(
        cls, value: dict[FindingCategory, Decimal] | None
    ) -> dict[FindingCategory, Decimal] | None:
        if value is not None:
            for weight in value.values():
                if not weight.is_finite() or weight <= 0:
                    raise ValueError("Category weights must be positive and finite")
        return value

    @field_validator("description", mode="before")
    @classmethod
    def trim_description(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class Week7TaskRewardCycleActionRequest(BaseModel):
    """Empty action body; clients cannot submit calculations or RewardEvents."""

    model_config = ConfigDict(extra="forbid")


class Week7RewardPolicySnapshot(BaseModel):
    """Immutable policy bundle committed by every new client-task cycle."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fingerprint_version: Literal["reward_cycle_fingerprint_v1"] = (
        WEEK7_REWARD_FINGERPRINT_VERSION
    )
    week7_cycle_policy_version: str
    week7_cycle_configuration_version: str
    task_budget_policy_version: str = TASK_REWARD_POLICY_VERSION
    task_budget_configuration_version: str
    task_reward_pool: TaskRewardPoolConfig
    finding_cluster_policy_version: str = FINDING_CLUSTER_POLICY_VERSION
    report_quality_policy_version: str
    report_quality_configuration_version: str
    report_quality: ReportQualityConfig
    task_finding_policy_version: str
    task_finding_configuration_version: str
    task_finding_reward: TaskFindingRewardConfig
    task_operator_policy_version: str
    task_operator_configuration_version: str
    task_operator_reward: TaskOperatorRewardConfig
    protocol_quantum: Decimal = PROTOCOL_POINTS_QUANTUM
    rounding_policy: Literal["largest_remainder_round_down_v1"] = (
        "largest_remainder_round_down_v1"
    )

    @field_validator("protocol_quantum", mode="before")
    @classmethod
    def validate_quantum(cls, value: Any) -> Decimal:
        quantum = _decimal(value, "protocol_quantum")
        if quantum != PROTOCOL_POINTS_QUANTUM:
            raise ValueError("Week 7 policy snapshot requires protocol quantum 0.000001")
        return quantum


class Week7TaskRewardCycle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reward_cycle_id: str
    cycle_version: Literal["week7_task_reward_cycle_v1"] = WEEK7_REWARD_CYCLE_VERSION
    policy_version: str = Field(..., min_length=1, max_length=128)
    configuration_version: str = Field(..., min_length=1, max_length=128)
    policy_snapshot: Week7RewardPolicySnapshot | None = None
    reward_domain: Literal[RewardDomain.CLIENT_TASK] = RewardDomain.CLIENT_TASK
    reward_unit: Literal[RewardUnit.PROTOCOL_POINTS] = RewardUnit.PROTOCOL_POINTS
    project_id: str
    routing_id: str
    routing_source_fingerprint: str
    task_reward_budget_id: str
    task_reward_budget_source_fingerprint: str
    status: RewardCycleStatus
    allocation_scope: FindingAllocationScope
    category_weights: dict[FindingCategory, Decimal]
    total_budget_points: Decimal
    miner_pool_points: Decimal
    validator_pool_points: Decimal
    protocol_pool_points: Decimal
    finding_calculation_id: str | None = None
    finding_calculation_source_fingerprint: str | None = None
    operator_calculation_id: str | None = None
    operator_calculation_source_fingerprint: str | None = None
    distributed_miner_points: Decimal
    undistributed_miner_points: Decimal
    reward_event_count: int = Field(..., ge=0)
    rewarded_operator_count: int = Field(..., ge=0)
    rewarded_cluster_count: int = Field(..., ge=0)
    chief_finder_count: int = Field(..., ge=0)
    reward_event_ids: list[str]
    request_fingerprint: str
    calculation_fingerprint: str | None = None
    finalization_source_fingerprint: str | None = None
    calculation_revision: int = Field(default=0, ge=0)
    superseded_calculation_fingerprints: list[str] = Field(default_factory=list)
    description: str | None = Field(default=None, max_length=500)
    created_at: datetime
    updated_at: datetime
    calculated_at: datetime | None = None
    finalized_at: datetime | None = None

    @field_validator("reward_cycle_id")
    @classmethod
    def validate_cycle_id(cls, value: str) -> str:
        if not CYCLE_ID.fullmatch(value):
            raise ValueError("Invalid Week 7 reward-cycle identifier")
        return value

    @field_validator(
        "project_id",
        "routing_id",
        "task_reward_budget_id",
        "finding_calculation_id",
        "operator_calculation_id",
    )
    @classmethod
    def validate_ids(cls, value: str | None, info) -> str | None:
        return None if value is None else _identifier(value, info.field_name)

    @field_validator(
        "routing_source_fingerprint",
        "task_reward_budget_source_fingerprint",
        "finding_calculation_source_fingerprint",
        "operator_calculation_source_fingerprint",
        "request_fingerprint",
        "calculation_fingerprint",
        "finalization_source_fingerprint",
    )
    @classmethod
    def validate_fingerprints(cls, value: str | None, info) -> str | None:
        return _fingerprint(value, info.field_name)

    @field_validator("superseded_calculation_fingerprints")
    @classmethod
    def validate_superseded_fingerprints(cls, value: list[str]) -> list[str]:
        if value != list(dict.fromkeys(value)):
            raise ValueError("Superseded calculation fingerprints must be unique")
        for fingerprint in value:
            _fingerprint(fingerprint, "superseded calculation fingerprint")
        return value

    @field_validator(
        "total_budget_points",
        "miner_pool_points",
        "validator_pool_points",
        "protocol_pool_points",
        "distributed_miner_points",
        "undistributed_miner_points",
        mode="before",
    )
    @classmethod
    def validate_points(cls, value: Any, info) -> Decimal:
        return _points(value, info.field_name, positive=info.field_name == "total_budget_points")

    @field_validator("category_weights", mode="before")
    @classmethod
    def reject_float_weights(cls, value: Any) -> Any:
        if isinstance(value, dict) and any(isinstance(item, float) for item in value.values()):
            raise ValueError("Category weights must use Decimal")
        return value

    @field_validator("category_weights")
    @classmethod
    def validate_cycle_category_weights(
        cls, value: dict[FindingCategory, Decimal]
    ) -> dict[FindingCategory, Decimal]:
        if any(not weight.is_finite() or weight <= 0 for weight in value.values()):
            raise ValueError("Cycle category weights must be positive and finite")
        return value

    @field_validator("reward_event_ids")
    @classmethod
    def validate_event_ids(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("RewardEvent identifiers must be unique and sorted")
        if any(not EVENT_ID.fullmatch(item) for item in value):
            raise ValueError("Invalid Week 7 RewardEvent identifier")
        return value

    @field_validator("created_at", "updated_at", "calculated_at", "finalized_at")
    @classmethod
    def validate_timestamps(cls, value: datetime | None, info) -> datetime | None:
        return _aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_cycle(self) -> "Week7TaskRewardCycle":
        if self.miner_pool_points + self.validator_pool_points + self.protocol_pool_points != self.total_budget_points:
            raise ValueError("Week 7 cycle budget snapshot must conserve total points")
        if self.distributed_miner_points + self.undistributed_miner_points != self.miner_pool_points:
            raise ValueError("Distributed plus undistributed miner points must equal miner pool")
        if self.reward_event_count != len(self.reward_event_ids):
            raise ValueError("RewardEvent count must match RewardEvent identifiers")
        if self.chief_finder_count > self.rewarded_cluster_count:
            raise ValueError("Chief count cannot exceed rewarded cluster count")
        required_calculation = (
            self.finding_calculation_id,
            self.finding_calculation_source_fingerprint,
            self.operator_calculation_id,
            self.operator_calculation_source_fingerprint,
            self.calculation_fingerprint,
            self.calculated_at,
        )
        if self.status == RewardCycleStatus.DRAFT:
            if any(item is not None for item in required_calculation):
                raise ValueError("Draft cycles cannot contain calculated state")
            if self.calculation_revision != 0 or self.distributed_miner_points != 0:
                raise ValueError("Draft cycles cannot distribute miner points")
            if self.undistributed_miner_points != self.miner_pool_points:
                raise ValueError("Draft cycle miner pool must remain undistributed")
            if self.finalized_at is not None or self.finalization_source_fingerprint is not None:
                raise ValueError("Draft cycles cannot contain finalization state")
        elif self.status == RewardCycleStatus.CALCULATED:
            if any(item is None for item in required_calculation):
                raise ValueError("Calculated cycles require complete calculation references")
            if self.calculation_revision < 1:
                raise ValueError("Calculated cycles require a calculation revision")
            if self.finalized_at is not None or self.finalization_source_fingerprint is not None:
                raise ValueError("Calculated cycles cannot contain finalization state")
            if self.reward_event_ids:
                raise ValueError("Calculated cycles cannot claim finalized RewardEvents")
        else:
            if any(item is None for item in required_calculation):
                raise ValueError("Finalized cycles require complete calculation references")
            if self.finalized_at is None or self.finalization_source_fingerprint is None:
                raise ValueError("Finalized cycles require finalization source state")
        return self


class Week7TaskRewardEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reward_event_id: str
    event_version: Literal["week7_task_reward_event_v1"] = WEEK7_REWARD_EVENT_VERSION
    event_status: Literal[RewardEventStatus.FINALIZED] = RewardEventStatus.FINALIZED
    reward_domain: Literal[RewardDomain.CLIENT_TASK] = RewardDomain.CLIENT_TASK
    reward_unit: Literal[RewardUnit.PROTOCOL_POINTS] = RewardUnit.PROTOCOL_POINTS
    reward_cycle_id: str
    project_id: str
    routing_id: str
    task_reward_budget_id: str
    finding_cluster_id: str
    category: FindingCategory
    final_severity: FindingSeverity
    severity_weight: Decimal
    distinct_operator_count: int = Field(..., ge=1)
    uniqueness: Decimal
    finding_score: Decimal
    finding_cluster_reward_points: Decimal
    operator_id: str
    node_id: str
    submission_id: str
    quality_assessment_id: str
    quality_score: Decimal
    quality_weight: Decimal
    quality_rank: int = Field(..., ge=1)
    chief_finder: bool
    chief_qualifying_submission_id: str | None = None
    quality_reward_points: Decimal
    chief_bonus_points: Decimal
    total_reward_points: Decimal
    policy_version: str = Field(..., min_length=1, max_length=128)
    cycle_finalization_fingerprint: str
    source_fingerprint: str
    created_at: datetime
    finalized_at: datetime

    @field_validator("reward_event_id")
    @classmethod
    def validate_event_id(cls, value: str) -> str:
        if not EVENT_ID.fullmatch(value):
            raise ValueError("Invalid Week 7 RewardEvent identifier")
        return value

    @field_validator(
        "reward_cycle_id",
        "project_id",
        "routing_id",
        "task_reward_budget_id",
        "finding_cluster_id",
        "operator_id",
        "node_id",
        "submission_id",
        "quality_assessment_id",
        "chief_qualifying_submission_id",
    )
    @classmethod
    def validate_ids(cls, value: str | None, info) -> str | None:
        return None if value is None else _identifier(value, info.field_name)

    @field_validator("category", mode="before")
    @classmethod
    def normalize_event_category(cls, value: Any) -> str:
        return normalize_category(value)

    @field_validator("severity_weight", "finding_score", mode="before")
    @classmethod
    def validate_nonnegative_decimal(cls, value: Any, info) -> Decimal:
        result = _decimal(value, info.field_name)
        if result < 0 or result != result.quantize(PROTOCOL_POINTS_QUANTUM):
            raise ValueError(
                f"{info.field_name} must be non-negative at six decimals"
            )
        return result

    @field_validator("uniqueness", "quality_score", mode="before")
    @classmethod
    def validate_ratios(cls, value: Any, info) -> Decimal:
        return _ratio(value, info.field_name)

    @field_validator("quality_weight", mode="before")
    @classmethod
    def validate_quality_weight(cls, value: Any) -> Decimal:
        result = _decimal(value, "quality_weight")
        if result < 0 or result > 1 or result != result.quantize(QUALITY_WEIGHT_QUANTUM):
            raise ValueError("quality_weight must be within [0,1] at twelve decimals")
        return result

    @field_validator(
        "finding_cluster_reward_points",
        "quality_reward_points",
        "chief_bonus_points",
        "total_reward_points",
        mode="before",
    )
    @classmethod
    def validate_event_points(cls, value: Any, info) -> Decimal:
        return _points(value, info.field_name, positive=info.field_name == "total_reward_points")

    @field_validator("cycle_finalization_fingerprint", "source_fingerprint")
    @classmethod
    def validate_event_fingerprints(cls, value: str, info) -> str:
        return _fingerprint(value, info.field_name)  # type: ignore[return-value]

    @field_validator("created_at", "finalized_at")
    @classmethod
    def validate_event_timestamps(cls, value: datetime, info) -> datetime:
        return _aware(value, info.field_name)  # type: ignore[return-value]

    @model_validator(mode="after")
    def validate_event(self) -> "Week7TaskRewardEvent":
        if self.quality_weight != self.quality_score * self.quality_score:
            raise ValueError("RewardEvent quality weight must equal Q squared")
        if self.total_reward_points != self.quality_reward_points + self.chief_bonus_points:
            raise ValueError("RewardEvent total must equal quality reward plus Chief bonus")
        if self.chief_finder:
            if self.chief_qualifying_submission_id is None:
                raise ValueError("Chief RewardEvent requires qualifying submission")
        elif self.chief_bonus_points != 0:
            raise ValueError("Non-Chief RewardEvent cannot contain Chief bonus")
        return self


class Week7TaskRewardCycleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    processing_status: Week7RewardProcessingStatus
    cycle: Week7TaskRewardCycle
    reward_events_created: int = Field(default=0, ge=0)
    reward_events_existing: int = Field(default=0, ge=0)
    message: str = Field(..., min_length=1)


class Week7TaskRewardCycleListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    total: int = Field(..., ge=0)
    cycles: list[Week7TaskRewardCycle]


class Week7TaskRewardEventListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int = Field(..., ge=0)
    events: list[Week7TaskRewardEvent]


class Week7TaskRewardHistorySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_type: Week7RewardHistorySubject
    subject_id: str
    total_reward_events: int = Field(..., ge=0)
    total_client_task_reward_points: Decimal
    rewarded_cluster_count: int = Field(..., ge=0)
    chief_finder_count: int = Field(..., ge=0)
    events: list[Week7TaskRewardEvent]

    @field_validator("subject_id")
    @classmethod
    def validate_subject_id(cls, value: str) -> str:
        return _identifier(value, "reward history subject")

    @field_validator("total_client_task_reward_points", mode="before")
    @classmethod
    def validate_total_points(cls, value: Any) -> Decimal:
        return _points(value, "total client task reward points")

    @model_validator(mode="after")
    def validate_history(self) -> "Week7TaskRewardHistorySummary":
        if self.total_reward_events != len(self.events):
            raise ValueError("Reward history event count does not match events")
        if self.total_client_task_reward_points != sum(
            (event.total_reward_points for event in self.events), Decimal("0")
        ):
            raise ValueError("Reward history total does not match events")
        if self.rewarded_cluster_count != len(
            {event.finding_cluster_id for event in self.events}
        ):
            raise ValueError("Rewarded cluster count does not match events")
        if self.chief_finder_count != sum(event.chief_finder for event in self.events):
            raise ValueError("Chief Finder count does not match events")
        return self


class Week7TaskRewardCycleVerification(BaseModel):
    """Read-only deterministic diagnosis of one cycle and its event ledger."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reward_cycle_id: str
    project_id: str
    routing_id: str
    lifecycle_status: RewardCycleStatus
    verification_status: Week7RewardVerificationStatus
    ok: bool
    source_fingerprint_match: bool | None
    calculation_fingerprint_match: bool | None
    budget_conserved: bool
    cluster_rewards_conserved: bool
    reporter_rewards_conserved: bool
    event_set_complete: bool
    duplicate_events_found: bool
    immutable_source_consistent: bool
    safe_retry_finalize: bool
    expected_event_count: int = Field(..., ge=0)
    stored_event_count: int = Field(..., ge=0)
    expected_event_total: Decimal
    stored_event_total: Decimal
    errors: list[str]

    @field_validator("reward_cycle_id", "project_id", "routing_id")
    @classmethod
    def validate_verification_ids(cls, value: str, info) -> str:
        return _identifier(value, info.field_name)

    @field_validator("expected_event_total", "stored_event_total", mode="before")
    @classmethod
    def validate_verification_points(cls, value: Any, info) -> Decimal:
        return _points(value, info.field_name)

    @field_validator("errors")
    @classmethod
    def validate_errors(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(not item for item in cleaned):
            raise ValueError("Verification errors must be non-empty")
        if cleaned != list(dict.fromkeys(cleaned)):
            raise ValueError("Verification errors must be unique and ordered")
        return cleaned
