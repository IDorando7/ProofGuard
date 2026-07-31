from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.finding import FindingCategory
from app.schemas.node import normalize_category
from app.schemas.routing import RoutingAssignmentMode, RoutingSelectionType
from app.schemas.subnet import SubnetMemberStatus


SUBNET_REWARD_VERSION = "subnet_reward_v0"
SUBNET_REWARD_POLICY_VERSION = "subnet_reward_policy_v0"
SUBNET_REWARD_EVENT_VERSION = "subnet_reward_event_v0"
MEMBERSHIP_MULTIPLIER_POLICY_VERSION = "membership_multiplier_policy_v0"
CATEGORY_MULTIPLIER_POLICY_VERSION = "category_multiplier_policy_v0"
PROTOCOL_POINTS_DECIMAL_PLACES = 6
PROTOCOL_POINTS_QUANTUM = Decimal("0.000001")
MAX_PROTOCOL_POINTS = Decimal("1000000000.000000")
SAFE_SUBNET_REWARD_IDENTIFIER = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_-]{0,255}$"
)
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


class SubnetRewardCycleStatus(str, Enum):
    DRAFT = "draft"
    CALCULATED = "calculated"
    FINALIZED = "finalized"


class SubnetRewardProcessingStatus(str, Enum):
    CREATED = "created"
    CALCULATED = "calculated"
    UNCHANGED = "unchanged"
    FINALIZED = "finalized"
    ALREADY_FINALIZED = "already_finalized"


class SubnetRewardEligibilityStatus(str, Enum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"


class SubnetRewardExclusionReason(str, Enum):
    ROUTING_NOT_FINALIZED = "routing_not_finalized"
    ROUTING_ASSIGNMENT_MISSING = "routing_assignment_missing"
    SUBMISSION_NOT_LINKED_TO_ROUTING = "submission_not_linked_to_routing"
    SUBMISSION_PROJECT_MISMATCH = "submission_project_mismatch"
    SUBMISSION_CATEGORY_MISMATCH = "submission_category_mismatch"
    SUBMISSION_NODE_MISMATCH = "submission_node_mismatch"
    SUBMISSION_NOT_FINALIZED = "submission_not_finalized"
    VALIDATION_NOT_ACCEPTED = "validation_not_accepted"
    FINDING_DUPLICATE = "finding_duplicate"
    FINDING_OUT_OF_SCOPE = "finding_out_of_scope"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    FINDING_REJECTED = "finding_rejected"
    UNSAFE_SUBMISSION = "unsafe_submission"
    UNSUPPORTED_SUBMISSION = "unsupported_submission"
    REPRODUCTION_NOT_REPRODUCED = "reproduction_not_reproduced"
    CONTRIBUTION_RECORD_MISSING = "contribution_record_missing"
    CONTRIBUTION_NOT_ELIGIBLE = "contribution_not_eligible"
    CONTRIBUTION_SCORE_ZERO = "contribution_score_zero"
    CATEGORY_SCORE_MISSING = "category_score_missing"
    MEMBERSHIP_SNAPSHOT_INELIGIBLE = "membership_snapshot_ineligible"
    CANDIDATE_SHADOW_INELIGIBLE = "candidate_shadow_ineligible"
    NODE_NOT_ACTIVE = "node_not_active"
    SOURCE_MISMATCH = "source_mismatch"
    ALREADY_REWARDED = "already_rewarded"
    CONFLICTING_REWARD_EVENT = "conflicting_reward_event"


class SubnetRewardEventStatus(str, Enum):
    APPLIED = "applied"


def _validate_identifier(value: str, label: str) -> str:
    if (
        not SAFE_SUBNET_REWARD_IDENTIFIER.fullmatch(value)
        or value in {".", ".."}
    ):
        raise ValueError(f"Invalid {label} identifier")
    return value


def _require_fingerprint(value: str, label: str = "Source") -> str:
    if not SHA256_HEX.fullmatch(value):
        raise ValueError(f"{label} fingerprint must be a lowercase SHA-256 digest")
    return value


def _require_aware(value: datetime | None) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("Subnet reward timestamps must be timezone-aware")
    return value


def _require_quantum(value: Decimal, label: str) -> Decimal:
    if value != value.quantize(PROTOCOL_POINTS_QUANTUM):
        raise ValueError(f"{label} must use at most six decimal places")
    return value


class SubnetRewardCycleCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    routing_id: str = Field(..., min_length=1, max_length=256)
    total_pool_points: Decimal = Field(..., gt=0, le=MAX_PROTOCOL_POINTS)
    category_weights: dict[FindingCategory, Decimal] | None = None
    description: str | None = Field(default=None, max_length=500)

    @field_validator("routing_id")
    @classmethod
    def validate_routing_id(cls, value: str) -> str:
        return _validate_identifier(value.strip(), "routing")

    @field_validator("total_pool_points", mode="before")
    @classmethod
    def reject_binary_float_pool(cls, value: Any) -> Any:
        if isinstance(value, float):
            raise ValueError("Protocol points must be supplied as a decimal string")
        return value

    @field_validator("total_pool_points")
    @classmethod
    def validate_pool_quantum(cls, value: Decimal) -> Decimal:
        return _require_quantum(value, "total_pool_points")

    @field_validator("category_weights", mode="before")
    @classmethod
    def normalize_weight_categories(cls, value: Any) -> Any:
        if value is None or not isinstance(value, dict):
            return value
        normalized: dict[str, Any] = {}
        for key, weight in value.items():
            category = normalize_category(key)
            if category in normalized:
                raise ValueError("Category-weight keys must be unique")
            if isinstance(weight, float):
                raise ValueError("Category weights must be supplied as decimal strings")
            normalized[category] = weight
        return normalized

    @field_validator("category_weights")
    @classmethod
    def validate_positive_weights(
        cls, value: dict[FindingCategory, Decimal] | None
    ) -> dict[FindingCategory, Decimal] | None:
        if value is not None and any(weight <= 0 for weight in value.values()):
            raise ValueError("Category weights must be positive")
        return value

    @field_validator("description", mode="before")
    @classmethod
    def trim_description(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class SubnetRewardActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SubnetRewardExclusion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    submission_id: str | None = Field(default=None, min_length=1, max_length=256)
    assignment_id: str | None = Field(default=None, min_length=1, max_length=256)
    node_id: str | None = Field(default=None, min_length=1, max_length=128)
    category: FindingCategory
    reasons: list[SubnetRewardExclusionReason] = Field(..., min_length=1)
    explanation: list[str] = Field(..., min_length=1)

    @field_validator("submission_id", "assignment_id", "node_id")
    @classmethod
    def validate_identifiers(cls, value: str | None) -> str | None:
        return (
            None
            if value is None
            else _validate_identifier(value, "subnet reward")
        )

    @field_validator("category", mode="before")
    @classmethod
    def normalize_exclusion_category(cls, value: Any) -> str:
        return normalize_category(value)

    @field_validator("reasons")
    @classmethod
    def validate_reasons(
        cls, value: list[SubnetRewardExclusionReason]
    ) -> list[SubnetRewardExclusionReason]:
        if len(value) != len(set(value)):
            raise ValueError("Exclusion reasons must be unique")
        return value

    @field_validator("explanation")
    @classmethod
    def validate_explanation(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(not item for item in cleaned):
            raise ValueError("Exclusion explanations must not be empty")
        return cleaned


class SubnetRewardSourceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    routing_id: str
    routing_source_fingerprint: str
    routing_assignment_id: str
    routing_assignment_membership_status: SubnetMemberStatus
    routing_assignment_category_score: Decimal = Field(..., ge=0, le=1)
    submission_id: str
    submission_source_fingerprint: str | None = None
    contribution_score_id: str
    contribution_source_fingerprint: str
    contribution_score: Decimal = Field(..., ge=0, le=100)
    validation_decision_id: str | None = None
    validation_source_fingerprint: str | None = None
    reproduction_result_id: str | None = None
    reproduction_source_fingerprint: str | None = None
    existing_reward_event_ids: list[str] = Field(default_factory=list)

    @field_validator(
        "routing_id",
        "routing_assignment_id",
        "submission_id",
        "contribution_score_id",
        "validation_decision_id",
        "reproduction_result_id",
    )
    @classmethod
    def validate_identifiers(cls, value: str | None) -> str | None:
        return (
            None
            if value is None
            else _validate_identifier(value, "reward source")
        )

    @field_validator(
        "routing_source_fingerprint",
        "submission_source_fingerprint",
        "contribution_source_fingerprint",
        "validation_source_fingerprint",
        "reproduction_source_fingerprint",
    )
    @classmethod
    def validate_fingerprints(cls, value: str | None) -> str | None:
        return None if value is None else _require_fingerprint(value)

    @field_validator("existing_reward_event_ids")
    @classmethod
    def validate_event_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("Existing reward event identifiers must be unique")
        if value != sorted(value):
            raise ValueError("Existing reward event identifiers must be sorted")
        return [_validate_identifier(item, "reward event") for item in value]


class SubnetSubmissionRewardAllocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allocation_id: str
    reward_cycle_id: str
    project_id: str
    routing_id: str
    routing_assignment_id: str
    submission_id: str
    finding_id: str
    node_id: str
    subnet_id: str
    category: FindingCategory
    selection_type: RoutingSelectionType
    assignment_mode: RoutingAssignmentMode
    membership_status: SubnetMemberStatus
    contribution_score: Decimal = Field(..., ge=0, le=100)
    category_score: Decimal = Field(..., ge=0, le=1)
    category_multiplier: Decimal = Field(..., gt=0)
    membership_multiplier: Decimal = Field(..., gt=0)
    raw_weight: Decimal = Field(..., gt=0)
    normalized_share: Decimal = Field(..., ge=0, le=1)
    reward_points: Decimal = Field(..., ge=0)
    eligibility_status: Literal[SubnetRewardEligibilityStatus.ELIGIBLE]
    eligibility_reasons: list[str] = Field(..., min_length=1)
    source_snapshot: SubnetRewardSourceSnapshot
    source_fingerprint: str
    created_at: datetime

    @field_validator(
        "allocation_id",
        "reward_cycle_id",
        "project_id",
        "routing_id",
        "routing_assignment_id",
        "submission_id",
        "finding_id",
        "node_id",
        "subnet_id",
    )
    @classmethod
    def validate_identifiers(cls, value: str) -> str:
        return _validate_identifier(value, "reward allocation")

    @field_validator("category", mode="before")
    @classmethod
    def normalize_allocation_category(cls, value: Any) -> str:
        return normalize_category(value)

    @field_validator("reward_points")
    @classmethod
    def validate_reward_quantum(cls, value: Decimal) -> Decimal:
        return _require_quantum(value, "reward_points")

    @field_validator("source_fingerprint")
    @classmethod
    def validate_fingerprint(cls, value: str) -> str:
        return _require_fingerprint(value)

    @field_validator("eligibility_reasons")
    @classmethod
    def validate_eligibility_reasons(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(not item for item in cleaned):
            raise ValueError("Eligibility explanations must not be empty")
        return cleaned

    @field_validator("created_at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        return _require_aware(value)  # type: ignore[return-value]

    @model_validator(mode="after")
    def validate_assignment_context(self) -> "SubnetSubmissionRewardAllocation":
        if self.subnet_id != f"subnet_{self.category.value}":
            raise ValueError("Allocation category must match subnet")
        if self.selection_type == RoutingSelectionType.RANKED:
            if (
                self.assignment_mode != RoutingAssignmentMode.PRODUCTION
                or self.membership_status
                not in {SubnetMemberStatus.ACTIVE, SubnetMemberStatus.EXPERT}
            ):
                raise ValueError("Ranked rewards require active/expert production")
        elif (
            self.assignment_mode != RoutingAssignmentMode.SHADOW
            or self.membership_status != SubnetMemberStatus.PROBATION
        ):
            raise ValueError("Exploration rewards require probation shadow")
        expected = (
            self.contribution_score
            * self.category_multiplier
            * self.membership_multiplier
        ).quantize(PROTOCOL_POINTS_QUANTUM)
        if self.raw_weight != expected:
            raise ValueError("raw_weight does not match the multiplier formula")
        snapshot = self.source_snapshot
        if (
            snapshot.routing_id != self.routing_id
            or snapshot.routing_assignment_id != self.routing_assignment_id
            or snapshot.submission_id != self.submission_id
            or snapshot.routing_assignment_membership_status
            != self.membership_status
            or snapshot.routing_assignment_category_score != self.category_score
            or snapshot.contribution_score != self.contribution_score
        ):
            raise ValueError("Allocation does not match its source snapshot")
        return self


class CategoryPoolAllocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: FindingCategory
    subnet_id: str
    requested_weight: Decimal = Field(..., gt=0)
    normalized_weight: Decimal = Field(..., gt=0, le=1)
    allocated_pool_points: Decimal = Field(..., ge=0)
    eligible_submissions: int = Field(..., ge=0)
    ineligible_submissions: int = Field(..., ge=0)
    total_raw_weight: Decimal = Field(..., ge=0)
    distributed_points: Decimal = Field(..., ge=0)
    undistributed_points: Decimal = Field(..., ge=0)
    allocations: list[SubnetSubmissionRewardAllocation]
    exclusions: list[SubnetRewardExclusion]
    warnings: list[str]

    @field_validator("category", mode="before")
    @classmethod
    def normalize_pool_category(cls, value: Any) -> str:
        return normalize_category(value)

    @field_validator("subnet_id")
    @classmethod
    def validate_subnet_id(cls, value: str) -> str:
        return _validate_identifier(value, "subnet")

    @field_validator(
        "allocated_pool_points",
        "distributed_points",
        "undistributed_points",
    )
    @classmethod
    def validate_point_quantum(cls, value: Decimal) -> Decimal:
        return _require_quantum(value, "category pool points")

    @field_validator("warnings")
    @classmethod
    def validate_warnings(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(not item for item in cleaned):
            raise ValueError("Category warnings must not be empty")
        return cleaned

    @model_validator(mode="after")
    def validate_pool_conservation(self) -> "CategoryPoolAllocation":
        if (
            self.distributed_points + self.undistributed_points
            != self.allocated_pool_points
        ):
            raise ValueError("Category pool does not conserve protocol points")
        if self.eligible_submissions != len(self.allocations):
            raise ValueError("Eligible count must match allocations")
        if self.ineligible_submissions != len(self.exclusions):
            raise ValueError("Ineligible count must match exclusions")
        if self.subnet_id != f"subnet_{self.category.value}":
            raise ValueError("Category pool must match its subnet")
        allocation_submissions = [item.submission_id for item in self.allocations]
        if len(allocation_submissions) != len(set(allocation_submissions)):
            raise ValueError("A submission may be allocated only once per category")
        for allocation in self.allocations:
            if (
                allocation.category != self.category
                or allocation.subnet_id != self.subnet_id
            ):
                raise ValueError("Allocation does not match its category pool")
        for exclusion in self.exclusions:
            if exclusion.category != self.category:
                raise ValueError("Exclusion does not match its category pool")
        return self


class NodeSubnetRewardSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    project_id: str
    reward_cycle_id: str
    total_reward_points: Decimal = Field(..., ge=0)
    production_reward_points: Decimal = Field(..., ge=0)
    shadow_reward_points: Decimal = Field(..., ge=0)
    rewarded_submissions: int = Field(..., ge=0)
    categories: list[FindingCategory]

    @field_validator("node_id", "project_id", "reward_cycle_id")
    @classmethod
    def validate_identifiers(cls, value: str) -> str:
        return _validate_identifier(value, "node reward summary")

    @field_validator(
        "total_reward_points",
        "production_reward_points",
        "shadow_reward_points",
    )
    @classmethod
    def validate_point_quantum(cls, value: Decimal) -> Decimal:
        return _require_quantum(value, "node reward points")

    @field_validator("categories", mode="before")
    @classmethod
    def normalize_categories(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        normalized = [normalize_category(item) for item in value]
        if normalized != sorted(set(normalized)):
            raise ValueError("Summary categories must be unique and sorted")
        return normalized

    @model_validator(mode="after")
    def validate_summary_totals(self) -> "NodeSubnetRewardSummary":
        if (
            self.production_reward_points + self.shadow_reward_points
            != self.total_reward_points
        ):
            raise ValueError("Node reward summary does not conserve points")
        return self


class SubnetRewardCycle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reward_cycle_id: str
    reward_version: Literal["subnet_reward_v0"] = SUBNET_REWARD_VERSION
    policy_version: Literal["subnet_reward_policy_v0"] = (
        SUBNET_REWARD_POLICY_VERSION
    )
    project_id: str
    routing_id: str
    routing_source_fingerprint: str
    status: SubnetRewardCycleStatus
    total_pool_points: Decimal = Field(..., gt=0, le=MAX_PROTOCOL_POINTS)
    total_distributed_points: Decimal = Field(..., ge=0)
    total_undistributed_points: Decimal = Field(..., ge=0)
    category_weights: dict[FindingCategory, Decimal]
    category_pools: list[CategoryPoolAllocation]
    node_summaries: list[NodeSubnetRewardSummary]
    request_fingerprint: str
    source_fingerprint: str | None = None
    description: str | None = Field(default=None, max_length=500)
    calculated_at: datetime | None = None
    finalized_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("reward_cycle_id", "project_id", "routing_id")
    @classmethod
    def validate_identifiers(cls, value: str) -> str:
        return _validate_identifier(value, "subnet reward")

    @field_validator(
        "routing_source_fingerprint",
        "request_fingerprint",
        "source_fingerprint",
    )
    @classmethod
    def validate_fingerprints(cls, value: str | None) -> str | None:
        return None if value is None else _require_fingerprint(value)

    @field_validator(
        "total_pool_points",
        "total_distributed_points",
        "total_undistributed_points",
    )
    @classmethod
    def validate_point_quantum(cls, value: Decimal) -> Decimal:
        return _require_quantum(value, "cycle protocol points")

    @field_validator("calculated_at", "finalized_at", "created_at", "updated_at")
    @classmethod
    def validate_timestamps(cls, value: datetime | None) -> datetime | None:
        return _require_aware(value)

    @model_validator(mode="after")
    def validate_cycle_lifecycle_and_totals(self) -> "SubnetRewardCycle":
        if (
            self.total_distributed_points + self.total_undistributed_points
            != self.total_pool_points
        ):
            raise ValueError("Reward cycle does not conserve protocol points")
        pool_total = sum(
            (item.allocated_pool_points for item in self.category_pools),
            Decimal("0"),
        )
        if pool_total != self.total_pool_points:
            raise ValueError("Category pools must sum to the complete cycle pool")
        categories = [item.category for item in self.category_pools]
        if categories != sorted(categories, key=lambda item: item.value):
            raise ValueError("Category pools must be sorted")
        if len(categories) != len(set(categories)):
            raise ValueError("Category pools must be unique")
        if set(self.category_weights) != set(categories):
            raise ValueError("Category weights must match category pools")
        distributed = sum(
            (item.distributed_points for item in self.category_pools),
            Decimal("0"),
        )
        if distributed != self.total_distributed_points:
            raise ValueError("Cycle distributed total does not match category pools")
        if self.status == SubnetRewardCycleStatus.DRAFT:
            if (
                self.source_fingerprint is not None
                or self.calculated_at is not None
                or self.finalized_at is not None
            ):
                raise ValueError("Draft cycles cannot have finalized source state")
        elif self.status == SubnetRewardCycleStatus.CALCULATED:
            if self.source_fingerprint is None or self.calculated_at is None:
                raise ValueError("Calculated cycles require source fingerprint and time")
            if self.finalized_at is not None:
                raise ValueError("Calculated cycles cannot have finalized_at")
        else:
            if (
                self.source_fingerprint is None
                or self.calculated_at is None
                or self.finalized_at is None
            ):
                raise ValueError("Finalized cycles require calculation and finalization")
        return self


class SubnetRewardEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reward_event_id: str
    reward_event_version: Literal["subnet_reward_event_v0"] = (
        SUBNET_REWARD_EVENT_VERSION
    )
    reward_cycle_id: str
    allocation_id: str
    project_id: str
    routing_id: str
    routing_assignment_id: str
    submission_id: str
    finding_id: str
    node_id: str
    subnet_id: str
    category: FindingCategory
    reward_points: Decimal = Field(..., ge=0)
    contribution_score: Decimal = Field(..., gt=0, le=100)
    category_multiplier: Decimal = Field(..., gt=0)
    membership_multiplier: Decimal = Field(..., gt=0)
    raw_weight: Decimal = Field(..., gt=0)
    source_fingerprint: str
    status: Literal[SubnetRewardEventStatus.APPLIED]
    applied_at: datetime

    @field_validator(
        "reward_event_id",
        "reward_cycle_id",
        "allocation_id",
        "project_id",
        "routing_id",
        "routing_assignment_id",
        "submission_id",
        "finding_id",
        "node_id",
        "subnet_id",
    )
    @classmethod
    def validate_identifiers(cls, value: str) -> str:
        return _validate_identifier(value, "subnet reward event")

    @field_validator("category", mode="before")
    @classmethod
    def normalize_event_category(cls, value: Any) -> str:
        return normalize_category(value)

    @field_validator("reward_points")
    @classmethod
    def validate_reward_quantum(cls, value: Decimal) -> Decimal:
        return _require_quantum(value, "event reward points")

    @field_validator("source_fingerprint")
    @classmethod
    def validate_source_fingerprint(cls, value: str) -> str:
        return _require_fingerprint(value)

    @field_validator("applied_at")
    @classmethod
    def validate_applied_at(cls, value: datetime) -> datetime:
        return _require_aware(value)  # type: ignore[return-value]

    @model_validator(mode="after")
    def validate_event_context(self) -> "SubnetRewardEvent":
        if self.subnet_id != f"subnet_{self.category.value}":
            raise ValueError("Reward event category must match subnet")
        return self


class SubnetRewardEligibilityResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: SubnetRewardEligibilityStatus
    reasons: list[SubnetRewardExclusionReason]
    explanations: list[str] = Field(..., min_length=1)

    @field_validator("reasons")
    @classmethod
    def validate_reasons(
        cls, value: list[SubnetRewardExclusionReason]
    ) -> list[SubnetRewardExclusionReason]:
        if len(value) != len(set(value)):
            raise ValueError("Eligibility reason codes must be unique")
        return value

    @field_validator("explanations")
    @classmethod
    def validate_explanations(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(not item for item in cleaned):
            raise ValueError("Eligibility explanations must not be empty")
        return cleaned

    @model_validator(mode="after")
    def validate_status_shape(self) -> "SubnetRewardEligibilityResult":
        if self.status == SubnetRewardEligibilityStatus.ELIGIBLE and self.reasons:
            raise ValueError("Eligible results cannot contain exclusion reasons")
        if self.status == SubnetRewardEligibilityStatus.INELIGIBLE and not self.reasons:
            raise ValueError("Ineligible results require exclusion reasons")
        return self


class SubnetRewardCycleCreateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: SubnetRewardProcessingStatus
    cycle: SubnetRewardCycle
    message: str = Field(..., min_length=1)


class SubnetRewardCalculationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: SubnetRewardProcessingStatus
    cycle: SubnetRewardCycle
    eligible_allocations: int = Field(..., ge=0)
    exclusions: int = Field(..., ge=0)
    message: str = Field(..., min_length=1)


class SubnetRewardFinalizationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: SubnetRewardProcessingStatus
    cycle: SubnetRewardCycle
    reward_events_created: int = Field(..., ge=0)
    reward_events_existing: int = Field(..., ge=0)
    message: str = Field(..., min_length=1)


class SubnetRewardEventListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int = Field(..., ge=0)
    events: list[SubnetRewardEvent]
