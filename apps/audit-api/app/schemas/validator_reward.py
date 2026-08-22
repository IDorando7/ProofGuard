from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.finding import FindingCategory
from app.schemas.reward import (
    RewardCycleStatus,
    RewardDomain,
    RewardEventStatus,
    RewardPoolKind,
    RewardUnit,
)
from app.schemas.subnet_reward import MAX_PROTOCOL_POINTS, PROTOCOL_POINTS_QUANTUM
from app.schemas.validator_attestation import ValidatorAssignmentRole


VALIDATOR_REWARD_POLICY_VERSION = "validator_reward_policy_v1"
VALIDATOR_REWARD_CYCLE_VERSION = "validator_reward_cycle_v1"
VALIDATOR_REWARD_ALLOCATION_VERSION = "validator_reward_allocation_v1"
VALIDATOR_REWARD_EVENT_VERSION = "validator_reward_event_v1"
REWARD_POOL_CONSUMPTION_VERSION = "reward_pool_consumption_v1"
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,255}$")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def _identifier(value: str, label: str) -> str:
    cleaned = value.strip()
    if not SAFE_IDENTIFIER.fullmatch(cleaned) or cleaned in {".", ".."} or "/" in cleaned or "\\" in cleaned:
        raise ValueError(f"Invalid {label} identifier")
    return cleaned


def _decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, float):
        raise ValueError(f"{label} must use Decimal or a decimal string")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a valid Decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{label} must be finite")
    return result


def _points(value: Any, label: str) -> Decimal:
    result = _decimal(value, label)
    if result < 0 or result > MAX_PROTOCOL_POINTS:
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


def _fingerprint(value: str, label: str) -> str:
    if not SHA256_HEX.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256 fingerprint")
    return value


def _aware(value: datetime | None, label: str) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError(f"{label} must be timezone-aware")
    return value


class ValidatorRewardUndistributedReason(str, Enum):
    INCOMPLETE_ASSIGNMENT = "incomplete_assignment"
    NO_FINAL_RESOLUTION = "no_final_resolution"
    ZERO_QUALITY = "zero_quality"
    QUALITY_REMAINDER = "quality_remainder"


class ValidatorRewardProcessingStatus(str, Enum):
    CREATED = "created"
    UNCHANGED = "unchanged"
    CALCULATED = "calculated"
    FINALIZED = "finalized"
    ALREADY_FINALIZED = "already_finalized"


class ValidatorRewardVerificationStatus(str, Enum):
    DRAFT = "draft"
    CALCULATED_NOT_FINALIZED = "calculated_not_finalized"
    STALE_CALCULATION = "stale_calculation"
    PARTIAL_EVENT_PUBLICATION = "partial_event_publication"
    EVENT_SET_COMPLETE_STATUS_NOT_FINALIZED = "event_set_complete_status_not_finalized"
    CLEAN_FINALIZED = "clean_finalized"
    FINALIZED_EVENT_MISMATCH = "finalized_event_mismatch"
    CORRUPT_REFERENCES = "corrupt_references"


class ValidatorRewardPolicyV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_version: Literal["validator_reward_policy_v1"] = VALIDATOR_REWARD_POLICY_VERSION
    completion_share: Decimal = Decimal("0.300000")
    quality_share: Decimal = Decimal("0.700000")
    quality_exponent: Literal[2] = 2
    shadow_reward_eligible: Literal[False] = False
    work_unit_weight: Decimal = Decimal("1.000000")
    quantum: Decimal = PROTOCOL_POINTS_QUANTUM

    @field_validator("completion_share", "quality_share", "work_unit_weight", "quantum", mode="before")
    @classmethod
    def decimals(cls, value, info):
        result = _decimal(value, info.field_name)
        if result != result.quantize(PROTOCOL_POINTS_QUANTUM):
            raise ValueError(f"{info.field_name} must use six-decimal precision")
        return result

    @model_validator(mode="after")
    def policy(self) -> "ValidatorRewardPolicyV1":
        if self.completion_share < 0 or self.quality_share < 0 or self.completion_share + self.quality_share != Decimal("1.000000"):
            raise ValueError("Completion and quality shares must sum exactly to one")
        if self.work_unit_weight != Decimal("1.000000"):
            raise ValueError("Validator reward v1 requires equal work-unit weight")
        if self.quantum != PROTOCOL_POINTS_QUANTUM:
            raise ValueError("Validator reward v1 requires protocol quantum 0.000001")
        return self


class ValidatorRewardCycleCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_reward_budget_id: str
    description: str | None = Field(default=None, max_length=500)

    @field_validator("task_reward_budget_id")
    @classmethod
    def budget_id(cls, value):
        return _identifier(value, "task reward budget")


class ValidatorRewardCycleActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ValidatorRewardAllocation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    validator_reward_allocation_id: str
    allocation_version: Literal["validator_reward_allocation_v1"] = VALIDATOR_REWARD_ALLOCATION_VERSION
    reward_policy_version: Literal["validator_reward_policy_v1"] = VALIDATOR_REWARD_POLICY_VERSION
    reward_cycle_id: str
    task_reward_budget_id: str
    project_id: str
    routing_id: str
    finding_cluster_id: str
    validation_round_id: str
    validator_committee_id: str
    validator_assignment_id: str
    validator_node_id: str
    validator_operator_id: str
    category: FindingCategory
    assignment_role: Literal[ValidatorAssignmentRole.AUTHORITATIVE] = ValidatorAssignmentRole.AUTHORITATIVE
    work_unit_weight: Decimal
    base_work_unit_budget: Decimal
    completion_eligible: bool
    completion_budget: Decimal
    completion_reward: Decimal
    final_validation_consensus_id: str | None = None
    validation_quality_assessment_id: str | None = None
    validation_quality_score: Decimal | None = None
    quality_squared_factor: Decimal | None = None
    quality_budget: Decimal
    quality_reward: Decimal
    total_reward: Decimal
    undistributed_points: Decimal
    undistributed_reason_codes: list[ValidatorRewardUndistributedReason]
    source_fingerprint: str

    @field_validator("validator_reward_allocation_id", "reward_cycle_id", "task_reward_budget_id", "project_id", "routing_id", "finding_cluster_id", "validation_round_id", "validator_committee_id", "validator_assignment_id", "validator_node_id", "validator_operator_id", "final_validation_consensus_id", "validation_quality_assessment_id")
    @classmethod
    def ids(cls, value, info):
        return None if value is None else _identifier(value, info.field_name)

    @field_validator("work_unit_weight", "base_work_unit_budget", "completion_budget", "completion_reward", "quality_budget", "quality_reward", "total_reward", "undistributed_points", mode="before")
    @classmethod
    def points(cls, value, info):
        return _points(value, info.field_name)

    @field_validator("validation_quality_score", "quality_squared_factor", mode="before")
    @classmethod
    def ratios(cls, value, info):
        return None if value is None else _ratio(value, info.field_name)

    @field_validator("undistributed_reason_codes", mode="before")
    @classmethod
    def reasons(cls, value):
        return sorted({ValidatorRewardUndistributedReason(item).value for item in value})

    @field_validator("source_fingerprint")
    @classmethod
    def fingerprint(cls, value):
        return _fingerprint(value, "allocation source")

    @model_validator(mode="after")
    def accounting(self) -> "ValidatorRewardAllocation":
        if self.completion_budget + self.quality_budget != self.base_work_unit_budget:
            raise ValueError("Completion plus quality budget must equal base work-unit budget")
        if self.completion_reward > self.completion_budget or self.quality_reward > self.quality_budget:
            raise ValueError("Reward component cannot exceed its budget")
        if self.total_reward != self.completion_reward + self.quality_reward:
            raise ValueError("Total reward must equal completion plus quality reward")
        if self.total_reward + self.undistributed_points != self.base_work_unit_budget:
            raise ValueError("Reward plus undistributed points must equal base budget")
        if self.validation_quality_score is None:
            if self.quality_squared_factor is not None or self.validation_quality_assessment_id is not None:
                raise ValueError("Quality references require a quality score")
        elif self.quality_squared_factor != (self.validation_quality_score * self.validation_quality_score).quantize(PROTOCOL_POINTS_QUANTUM):
            raise ValueError("Quality factor must equal VQ squared")
        return self


class ValidatorRewardCycle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reward_cycle_id: str
    cycle_version: Literal["validator_reward_cycle_v1"] = VALIDATOR_REWARD_CYCLE_VERSION
    reward_policy: ValidatorRewardPolicyV1
    reward_domain: Literal[RewardDomain.CLIENT_TASK] = RewardDomain.CLIENT_TASK
    reward_unit: Literal[RewardUnit.PROTOCOL_POINTS] = RewardUnit.PROTOCOL_POINTS
    pool_kind: Literal[RewardPoolKind.VALIDATOR] = RewardPoolKind.VALIDATOR
    project_id: str
    routing_id: str
    routing_source_fingerprint: str
    task_reward_budget_id: str
    task_reward_budget_source_fingerprint: str
    validator_pool_points: Decimal
    status: RewardCycleStatus
    allocations: list[ValidatorRewardAllocation]
    authoritative_work_units: int = Field(..., ge=0)
    completed_work_units: int = Field(..., ge=0)
    resolved_quality_units: int = Field(..., ge=0)
    unresolved_units: int = Field(..., ge=0)
    distributed_validator_points: Decimal
    undistributed_validator_points: Decimal
    reward_event_count: int = Field(..., ge=0)
    reward_event_ids: list[str]
    request_fingerprint: str
    source_fingerprint: str | None = None
    calculation_fingerprint: str | None = None
    finalization_source_fingerprint: str | None = None
    calculation_revision: int = Field(default=0, ge=0)
    description: str | None = Field(default=None, max_length=500)
    created_at: datetime
    updated_at: datetime
    calculated_at: datetime | None = None
    finalized_at: datetime | None = None

    @field_validator("reward_cycle_id", "project_id", "routing_id", "task_reward_budget_id")
    @classmethod
    def ids(cls, value, info):
        return _identifier(value, info.field_name)

    @field_validator("routing_source_fingerprint", "task_reward_budget_source_fingerprint", "request_fingerprint", "source_fingerprint", "calculation_fingerprint", "finalization_source_fingerprint")
    @classmethod
    def fingerprints(cls, value, info):
        return None if value is None else _fingerprint(value, info.field_name)

    @field_validator("validator_pool_points", "distributed_validator_points", "undistributed_validator_points", mode="before")
    @classmethod
    def points(cls, value, info):
        return _points(value, info.field_name)

    @field_validator("created_at", "updated_at", "calculated_at", "finalized_at")
    @classmethod
    def times(cls, value, info):
        return _aware(value, info.field_name)

    @model_validator(mode="after")
    def lifecycle(self) -> "ValidatorRewardCycle":
        if self.authoritative_work_units != len(self.allocations):
            raise ValueError("Work-unit count must equal allocations")
        if self.completed_work_units > self.authoritative_work_units or self.resolved_quality_units > self.completed_work_units or self.unresolved_units > self.authoritative_work_units:
            raise ValueError("Validator reward summary counts are inconsistent")
        if self.distributed_validator_points + self.undistributed_validator_points != self.validator_pool_points:
            raise ValueError("Validator reward accounting must conserve validator pool")
        base_total = sum((item.base_work_unit_budget for item in self.allocations), Decimal("0"))
        allocation_distributed = sum((item.total_reward for item in self.allocations), Decimal("0"))
        allocation_undistributed = sum((item.undistributed_points for item in self.allocations), Decimal("0"))
        if self.allocations:
            if base_total != self.validator_pool_points:
                raise ValueError("Work-unit base budgets must conserve validator pool")
        elif self.undistributed_validator_points != self.validator_pool_points:
            raise ValueError("Zero-work cycle must leave its validator pool undistributed")
        if allocation_distributed != self.distributed_validator_points:
            raise ValueError("Allocation rewards must equal cycle distributed total")
        if self.allocations and allocation_undistributed != self.undistributed_validator_points:
            raise ValueError("Allocation remainder must equal cycle undistributed total")
        if self.reward_event_count != len(self.reward_event_ids):
            raise ValueError("RewardEvent count must match identifiers")
        calculated = (self.source_fingerprint, self.calculation_fingerprint, self.calculated_at)
        if self.status == RewardCycleStatus.DRAFT:
            if any(value is not None for value in calculated) or self.allocations or self.calculation_revision != 0:
                raise ValueError("Draft cycle cannot contain calculated state")
            if self.completed_work_units or self.resolved_quality_units or self.unresolved_units or self.distributed_validator_points or self.reward_event_ids:
                raise ValueError("Draft cycle cannot contain reward results")
        elif any(value is None for value in calculated):
            raise ValueError("Calculated cycle requires fingerprints and calculated_at")
        if self.status == RewardCycleStatus.CALCULATED and self.reward_event_ids:
            raise ValueError("Calculated cycle cannot claim finalized RewardEvents")
        if self.status == RewardCycleStatus.FINALIZED:
            if self.finalized_at is None or self.finalization_source_fingerprint is None:
                raise ValueError("Finalized cycle requires finalization state")
            if self.reward_event_count != sum(item.total_reward > 0 for item in self.allocations):
                raise ValueError("Finalized RewardEvent count must match positive allocations")
        elif self.finalized_at is not None or self.finalization_source_fingerprint is not None:
            raise ValueError("Only finalized cycle may contain finalization state")
        return self


class ValidatorRewardEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reward_event_id: str
    event_version: Literal["validator_reward_event_v1"] = VALIDATOR_REWARD_EVENT_VERSION
    event_type: Literal["client_task_validator_reward"] = "client_task_validator_reward"
    event_status: Literal[RewardEventStatus.FINALIZED] = RewardEventStatus.FINALIZED
    reward_domain: Literal[RewardDomain.CLIENT_TASK] = RewardDomain.CLIENT_TASK
    reward_unit: Literal[RewardUnit.PROTOCOL_POINTS] = RewardUnit.PROTOCOL_POINTS
    pool_kind: Literal[RewardPoolKind.VALIDATOR] = RewardPoolKind.VALIDATOR
    reward_cycle_id: str
    task_reward_budget_id: str
    project_id: str
    routing_id: str
    finding_cluster_id: str
    validation_round_id: str
    validator_assignment_id: str
    validator_node_id: str
    validator_operator_id: str
    completion_reward: Decimal
    quality_reward: Decimal
    total_reward: Decimal
    validation_quality_assessment_id: str | None = None
    allocation_fingerprint: str
    cycle_finalization_fingerprint: str
    source_fingerprint: str
    created_at: datetime
    finalized_at: datetime

    @field_validator("reward_event_id", "reward_cycle_id", "task_reward_budget_id", "project_id", "routing_id", "finding_cluster_id", "validation_round_id", "validator_assignment_id", "validator_node_id", "validator_operator_id", "validation_quality_assessment_id")
    @classmethod
    def ids(cls, value, info):
        return None if value is None else _identifier(value, info.field_name)

    @field_validator("completion_reward", "quality_reward", "total_reward", mode="before")
    @classmethod
    def points(cls, value, info):
        return _points(value, info.field_name)

    @field_validator("allocation_fingerprint", "cycle_finalization_fingerprint", "source_fingerprint")
    @classmethod
    def fingerprints(cls, value, info):
        return _fingerprint(value, info.field_name)

    @field_validator("created_at", "finalized_at")
    @classmethod
    def times(cls, value, info):
        return _aware(value, info.field_name)

    @model_validator(mode="after")
    def total(self) -> "ValidatorRewardEvent":
        if self.total_reward <= 0 or self.total_reward != self.completion_reward + self.quality_reward:
            raise ValueError("Validator RewardEvent must have a positive reconciled total")
        return self


class RewardPoolConsumption(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    reward_pool_consumption_id: str
    consumption_version: Literal["reward_pool_consumption_v1"] = REWARD_POOL_CONSUMPTION_VERSION
    task_reward_budget_id: str
    pool_kind: RewardPoolKind
    reward_cycle_id: str
    consumed_points: Decimal
    source_fingerprint: str
    finalized_at: datetime

    @field_validator("reward_pool_consumption_id", "task_reward_budget_id", "reward_cycle_id")
    @classmethod
    def ids(cls, value, info):
        return _identifier(value, info.field_name)

    @field_validator("consumed_points", mode="before")
    @classmethod
    def points(cls, value):
        return _points(value, "consumed_points")

    @field_validator("source_fingerprint")
    @classmethod
    def fingerprint(cls, value):
        return _fingerprint(value, "consumption source")

    @field_validator("finalized_at")
    @classmethod
    def time(cls, value):
        return _aware(value, "finalized_at")


class ValidatorRewardCycleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    processing_status: ValidatorRewardProcessingStatus
    cycle: ValidatorRewardCycle
    reward_events_created: int = Field(default=0, ge=0)
    reward_events_existing: int = Field(default=0, ge=0)
    message: str


class ValidatorRewardCycleListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total: int = Field(..., ge=0)
    cycles: list[ValidatorRewardCycle]


class ValidatorRewardAllocationListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total: int = Field(..., ge=0)
    allocations: list[ValidatorRewardAllocation]


class ValidatorRewardEventListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total: int = Field(..., ge=0)
    events: list[ValidatorRewardEvent]


class ValidatorRewardCycleVerification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    reward_cycle_id: str
    verification_status: ValidatorRewardVerificationStatus
    ok: bool
    source_fingerprint_match: bool | None
    calculation_fingerprint_match: bool | None
    accounting_conserved: bool
    work_snapshot_closed: bool
    event_set_complete: bool
    pool_consumption_consistent: bool
    safe_retry_finalize: bool
    expected_event_count: int = Field(..., ge=0)
    stored_event_count: int = Field(..., ge=0)
    errors: list[str]
