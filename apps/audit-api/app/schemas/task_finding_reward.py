from __future__ import annotations

import re
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.finding import FindingCategory, FindingSeverity
from app.schemas.node import normalize_category
from app.schemas.reward import RewardDomain, RewardUnit
from app.schemas.subnet_reward import MAX_PROTOCOL_POINTS, PROTOCOL_POINTS_QUANTUM


TASK_FINDING_CALCULATION_VERSION = "task_finding_reward_calculation_v1"
TASK_FINDING_POLICY_VERSION = "finding_cluster_value_v1"
TASK_FINDING_CONFIGURATION_VERSION = "task_finding_allocation_config_v1"
UNIQUENESS_QUANTUM = Decimal("0.000001")
FINDING_SCORE_QUANTUM = Decimal("0.000001")
NORMALIZED_WEIGHT_QUANTUM = Decimal("0.000000000001")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
CALCULATION_ID = re.compile(r"^task_finding_reward_calculation_[0-9a-f]{32}$")
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,255}$")


class FindingAllocationScope(str, Enum):
    GLOBAL = "global"
    CATEGORY_ISOLATED = "category_isolated"


class FindingParentPoolType(str, Enum):
    MINER_POOL = "miner_pool"
    CATEGORY_POOL = "category_pool"


class FindingClusterAllocationReason(str, Enum):
    POSITIVE_FINDING_SCORE = "positive_finding_score"
    ZERO_SEVERITY_WEIGHT = "zero_severity_weight"


class TaskFindingCalculationStatus(str, Enum):
    CALCULATED = "calculated"
    SUPERSEDED = "superseded"


class TaskFindingCalculationOutcome(str, Enum):
    ALLOCATED = "allocated"
    PARTIALLY_ALLOCATED = "partially_allocated"
    NO_ELIGIBLE_FINDINGS = "no_eligible_findings"
    NO_POSITIVE_FINDING_SCORE = "no_positive_finding_score"


class TaskFindingCalculationProcessingStatus(str, Enum):
    CALCULATED = "calculated"
    UNCHANGED = "unchanged"
    SUPERSEDED = "superseded"


def _decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, float):
        raise ValueError(f"{label} must be supplied as a Decimal or decimal string")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a valid decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{label} must be finite")
    return result


def _bounded_ratio(value: Any, label: str) -> Decimal:
    result = _decimal(value, label)
    if result < 0 or result > 1:
        raise ValueError(f"{label} must be between 0 and 1")
    if result != result.quantize(UNIQUENESS_QUANTUM):
        raise ValueError(f"{label} must use at most six decimal places")
    return result


def _points(value: Any, label: str) -> Decimal:
    result = _decimal(value, label)
    if result < 0 or result > MAX_PROTOCOL_POINTS:
        raise ValueError(f"{label} is outside the protocol-points range")
    if result != result.quantize(PROTOCOL_POINTS_QUANTUM):
        raise ValueError(f"{label} must use at most six decimal places")
    return result


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


class SeverityRewardWeightConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    critical: Decimal = Decimal("16")
    high: Decimal = Decimal("8")
    medium: Decimal = Decimal("3")
    low: Decimal = Decimal("1")
    informational: Decimal = Decimal("0")

    @field_validator("*", mode="before")
    @classmethod
    def validate_weights(cls, value: Any, info) -> Decimal:
        weight = _decimal(value, f"{info.field_name} severity weight")
        if weight < 0:
            raise ValueError("Severity reward weights cannot be negative")
        if weight != weight.quantize(FINDING_SCORE_QUANTUM):
            raise ValueError("Severity reward weights use at most six decimal places")
        return weight


class UniquenessRewardConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    coefficient: Decimal = Decimal("0.20")
    floor: Decimal = Decimal("0.50")

    @field_validator("coefficient", mode="before")
    @classmethod
    def validate_coefficient(cls, value: Any) -> Decimal:
        coefficient = _decimal(value, "uniqueness coefficient")
        if coefficient < 0:
            raise ValueError("Uniqueness coefficient cannot be negative")
        if coefficient != coefficient.quantize(UNIQUENESS_QUANTUM):
            raise ValueError("Uniqueness coefficient uses at most six decimal places")
        return coefficient

    @field_validator("floor", mode="before")
    @classmethod
    def validate_floor(cls, value: Any) -> Decimal:
        return _bounded_ratio(value, "uniqueness floor")


class TaskFindingRewardConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    severity_weights: SeverityRewardWeightConfig = SeverityRewardWeightConfig()
    uniqueness: UniquenessRewardConfig = UniquenessRewardConfig()
    default_allocation_scope: FindingAllocationScope = (
        FindingAllocationScope.CATEGORY_ISOLATED
    )


class TaskFindingRewardCalculationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_reward_budget_id: str = Field(..., min_length=1, max_length=256)
    allocation_scope: FindingAllocationScope | None = None
    category_weights: dict[FindingCategory, Decimal] | None = None

    @field_validator("task_reward_budget_id")
    @classmethod
    def validate_budget_id(cls, value: str) -> str:
        return _identifier(value, "task reward budget")

    @field_validator("category_weights", mode="before")
    @classmethod
    def normalize_category_weights(cls, value: Any) -> Any:
        if value is None or not isinstance(value, dict):
            return value
        normalized: dict[str, Any] = {}
        for category, weight in value.items():
            name = normalize_category(category)
            if name in normalized:
                raise ValueError("Category weights must be unique")
            if isinstance(weight, float):
                raise ValueError("Category weights must use decimal strings")
            normalized[name] = weight
        return normalized

    @field_validator("category_weights")
    @classmethod
    def validate_category_weights(
        cls, value: dict[FindingCategory, Decimal] | None
    ) -> dict[FindingCategory, Decimal] | None:
        if value is not None:
            for weight in value.values():
                if not weight.is_finite() or weight <= 0:
                    raise ValueError("Category weights must be positive and finite")
        return value

    @model_validator(mode="after")
    def validate_scope_shape(self) -> "TaskFindingRewardCalculationRequest":
        if (
            self.allocation_scope == FindingAllocationScope.GLOBAL
            and self.category_weights is not None
        ):
            raise ValueError("Global finding allocation does not accept category weights")
        return self


class FindingClusterValue(BaseModel):
    """Pure cluster-level economic input before any parent-pool allocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    finding_cluster_id: str
    project_id: str
    routing_id: str
    category: FindingCategory
    cluster_source_fingerprint: str
    final_severity: FindingSeverity
    severity_weight: Decimal
    distinct_operator_count: int = Field(..., ge=1)
    uniqueness: Decimal
    finding_score: Decimal
    allocation_reason: FindingClusterAllocationReason

    @field_validator("finding_cluster_id", "project_id", "routing_id")
    @classmethod
    def validate_ids(cls, value: str, info) -> str:
        return _identifier(value, info.field_name)

    @field_validator("category", mode="before")
    @classmethod
    def normalize_value_category(cls, value: Any) -> str:
        return normalize_category(value)

    @field_validator("cluster_source_fingerprint")
    @classmethod
    def validate_cluster_fingerprint(cls, value: str) -> str:
        if not SHA256_HEX.fullmatch(value):
            raise ValueError("Cluster source fingerprint must be SHA-256")
        return value

    @field_validator("severity_weight", "finding_score", mode="before")
    @classmethod
    def validate_score_decimals(cls, value: Any, info) -> Decimal:
        score = _decimal(value, info.field_name)
        if score < 0:
            raise ValueError(f"{info.field_name} cannot be negative")
        if score != score.quantize(FINDING_SCORE_QUANTUM):
            raise ValueError(f"{info.field_name} uses at most six decimal places")
        return score

    @field_validator("uniqueness", mode="before")
    @classmethod
    def validate_uniqueness(cls, value: Any) -> Decimal:
        return _bounded_ratio(value, "uniqueness")

    @model_validator(mode="after")
    def validate_value_formula(self) -> "FindingClusterValue":
        expected = (self.severity_weight * self.uniqueness).quantize(
            FINDING_SCORE_QUANTUM, rounding=ROUND_HALF_EVEN
        )
        if self.finding_score != expected:
            raise ValueError("FindingScore must equal severity weight times uniqueness")
        if self.allocation_reason == FindingClusterAllocationReason.ZERO_SEVERITY_WEIGHT:
            if self.severity_weight != 0 or self.finding_score != 0:
                raise ValueError("Zero-weight reason requires zero severity weight and score")
        elif self.finding_score <= 0:
            raise ValueError("Positive-score reason requires a positive FindingScore")
        return self


class FindingClusterRewardAllocation(FindingClusterValue):
    model_config = ConfigDict(extra="forbid", frozen=True)

    parent_pool_type: FindingParentPoolType
    parent_pool_id: str
    parent_pool_points: Decimal
    cluster_reward_points: Decimal
    source_fingerprint: str

    @field_validator("parent_pool_id")
    @classmethod
    def validate_parent_pool_id(cls, value: str) -> str:
        return _identifier(value, "parent pool")

    @field_validator("parent_pool_points", "cluster_reward_points", mode="before")
    @classmethod
    def validate_points(cls, value: Any, info) -> Decimal:
        return _points(value, info.field_name)

    @field_validator("source_fingerprint")
    @classmethod
    def validate_source_fingerprint(cls, value: str) -> str:
        if not SHA256_HEX.fullmatch(value):
            raise ValueError("Allocation fingerprint must be SHA-256")
        return value

    @model_validator(mode="after")
    def validate_reward_shape(self) -> "FindingClusterRewardAllocation":
        if self.cluster_reward_points > self.parent_pool_points:
            raise ValueError("Cluster reward cannot exceed its parent pool")
        if self.finding_score == 0 and self.cluster_reward_points != 0:
            raise ValueError("Zero-score clusters cannot receive protocol points")
        return self


class FindingCategoryPoolAllocation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    category: FindingCategory
    parent_pool_id: str
    requested_weight: Decimal = Field(..., gt=0)
    normalized_weight: Decimal = Field(..., gt=0, le=1)
    allocated_pool_points: Decimal
    distributed_cluster_points: Decimal
    undistributed_cluster_points: Decimal
    eligible_cluster_count: int = Field(..., ge=0)
    positive_score_cluster_count: int = Field(..., ge=0)
    zero_weight_cluster_count: int = Field(..., ge=0)
    finding_cluster_ids: list[str]

    @field_validator("category", mode="before")
    @classmethod
    def normalize_pool_category(cls, value: Any) -> str:
        return normalize_category(value)

    @field_validator("parent_pool_id")
    @classmethod
    def validate_parent_pool_id(cls, value: str) -> str:
        return _identifier(value, "category parent pool")

    @field_validator("requested_weight", "normalized_weight", mode="before")
    @classmethod
    def validate_weights(cls, value: Any, info) -> Decimal:
        result = _decimal(value, info.field_name)
        if result <= 0:
            raise ValueError("Category pool weights must be positive")
        return result

    @field_validator(
        "allocated_pool_points",
        "distributed_cluster_points",
        "undistributed_cluster_points",
        mode="before",
    )
    @classmethod
    def validate_pool_points(cls, value: Any, info) -> Decimal:
        return _points(value, info.field_name)

    @field_validator("finding_cluster_ids")
    @classmethod
    def validate_cluster_ids(cls, value: list[str]) -> list[str]:
        cleaned = [_identifier(item, "finding cluster") for item in value]
        if cleaned != sorted(set(cleaned)):
            raise ValueError("Category cluster IDs must be unique and sorted")
        return cleaned

    @model_validator(mode="after")
    def validate_pool_conservation(self) -> "FindingCategoryPoolAllocation":
        if (
            self.distributed_cluster_points + self.undistributed_cluster_points
            != self.allocated_pool_points
        ):
            raise ValueError("Finding category pool must conserve protocol points")
        if self.eligible_cluster_count != len(self.finding_cluster_ids):
            raise ValueError("Eligible category cluster count does not match IDs")
        if (
            self.positive_score_cluster_count + self.zero_weight_cluster_count
            != self.eligible_cluster_count
        ):
            raise ValueError("Category cluster score counts do not match eligible count")
        return self


class TaskFindingRewardCalculation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    calculation_id: str
    calculation_version: Literal["task_finding_reward_calculation_v1"] = (
        TASK_FINDING_CALCULATION_VERSION
    )
    policy_version: str = Field(
        default=TASK_FINDING_POLICY_VERSION, min_length=1, max_length=128
    )
    configuration_version: str = Field(..., min_length=1, max_length=128)
    status: TaskFindingCalculationStatus
    outcome: TaskFindingCalculationOutcome
    project_id: str
    routing_id: str
    task_reward_budget_id: str
    task_reward_budget_source_fingerprint: str
    reward_domain: Literal[RewardDomain.CLIENT_TASK] = RewardDomain.CLIENT_TASK
    reward_unit: Literal[RewardUnit.PROTOCOL_POINTS] = RewardUnit.PROTOCOL_POINTS
    allocation_scope: FindingAllocationScope
    miner_pool_points: Decimal
    validator_pool_points_snapshot: Decimal
    protocol_pool_points_snapshot: Decimal
    severity_weights: SeverityRewardWeightConfig
    uniqueness_config: UniquenessRewardConfig
    category_weights: dict[FindingCategory, Decimal]
    category_pool_allocations: list[FindingCategoryPoolAllocation]
    cluster_allocations: list[FindingClusterRewardAllocation]
    total_finding_score: Decimal
    eligible_cluster_count: int = Field(..., ge=0)
    positive_score_cluster_count: int = Field(..., ge=0)
    zero_weight_cluster_count: int = Field(..., ge=0)
    distributed_cluster_points: Decimal
    undistributed_cluster_points: Decimal
    source_fingerprint: str
    supersedes_calculation_id: str | None = None
    superseded_by_calculation_id: str | None = None
    created_at: datetime
    calculated_at: datetime

    @field_validator(
        "calculation_id", "supersedes_calculation_id", "superseded_by_calculation_id"
    )
    @classmethod
    def validate_calculation_ids(cls, value: str | None) -> str | None:
        if value is not None and not CALCULATION_ID.fullmatch(value):
            raise ValueError("Invalid task finding calculation identifier")
        return value

    @field_validator("project_id", "routing_id", "task_reward_budget_id")
    @classmethod
    def validate_ids(cls, value: str, info) -> str:
        return _identifier(value, info.field_name)

    @field_validator("task_reward_budget_source_fingerprint", "source_fingerprint")
    @classmethod
    def validate_fingerprints(cls, value: str) -> str:
        if not SHA256_HEX.fullmatch(value):
            raise ValueError("Calculation fingerprint must be SHA-256")
        return value

    @field_validator(
        "miner_pool_points",
        "validator_pool_points_snapshot",
        "protocol_pool_points_snapshot",
        "distributed_cluster_points",
        "undistributed_cluster_points",
        mode="before",
    )
    @classmethod
    def validate_points(cls, value: Any, info) -> Decimal:
        return _points(value, info.field_name)

    @field_validator("total_finding_score", mode="before")
    @classmethod
    def validate_total_score(cls, value: Any) -> Decimal:
        score = _decimal(value, "total_finding_score")
        if score < 0 or score != score.quantize(FINDING_SCORE_QUANTUM):
            raise ValueError("Total FindingScore must be non-negative at score precision")
        return score

    @field_validator("category_weights", mode="before")
    @classmethod
    def normalize_record_category_weights(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        return {normalize_category(key): weight for key, weight in value.items()}

    @field_validator("category_weights")
    @classmethod
    def validate_record_category_weights(
        cls, value: dict[FindingCategory, Decimal]
    ) -> dict[FindingCategory, Decimal]:
        if any(not weight.is_finite() or weight <= 0 for weight in value.values()):
            raise ValueError("Category weights must be positive and finite")
        return value

    @field_validator("created_at", "calculated_at")
    @classmethod
    def validate_timestamps(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Task finding timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_calculation(self) -> "TaskFindingRewardCalculation":
        if (
            self.distributed_cluster_points + self.undistributed_cluster_points
            != self.miner_pool_points
        ):
            raise ValueError("Finding allocation must conserve the task miner pool")
        if self.cluster_allocations != sorted(
            self.cluster_allocations, key=lambda item: item.finding_cluster_id
        ):
            raise ValueError("Cluster allocations must be deterministically sorted")
        cluster_ids = [item.finding_cluster_id for item in self.cluster_allocations]
        if len(cluster_ids) != len(set(cluster_ids)):
            raise ValueError("Each FindingCluster may be allocated only once")
        if self.eligible_cluster_count != len(self.cluster_allocations):
            raise ValueError("Eligible cluster count does not match allocations")
        positive = sum(item.finding_score > 0 for item in self.cluster_allocations)
        zero = len(self.cluster_allocations) - positive
        if (
            positive != self.positive_score_cluster_count
            or zero != self.zero_weight_cluster_count
        ):
            raise ValueError("FindingScore counts do not match allocations")
        if self.total_finding_score != sum(
            (item.finding_score for item in self.cluster_allocations), Decimal("0")
        ).quantize(FINDING_SCORE_QUANTUM):
            raise ValueError("Total FindingScore does not match cluster allocations")
        if self.distributed_cluster_points != sum(
            (item.cluster_reward_points for item in self.cluster_allocations),
            Decimal("0"),
        ):
            raise ValueError("Distributed total does not match cluster allocations")
        if self.allocation_scope == FindingAllocationScope.GLOBAL:
            if self.category_pool_allocations or self.category_weights:
                raise ValueError("Global allocation cannot contain category pools")
            if any(
                item.parent_pool_type != FindingParentPoolType.MINER_POOL
                or item.parent_pool_id != self.task_reward_budget_id
                or item.parent_pool_points != self.miner_pool_points
                for item in self.cluster_allocations
            ):
                raise ValueError("Global cluster allocations must use the task miner pool")
        else:
            if self.category_pool_allocations != sorted(
                self.category_pool_allocations, key=lambda item: item.category.value
            ):
                raise ValueError("Category pools must be deterministically sorted")
            if set(self.category_weights) != {
                item.category for item in self.category_pool_allocations
            }:
                raise ValueError("Category weights must match category pools")
            if sum(
                (item.allocated_pool_points for item in self.category_pool_allocations),
                Decimal("0"),
            ) != self.miner_pool_points:
                raise ValueError("Category pools must sum to the miner pool")
            if sum(
                (item.distributed_cluster_points for item in self.category_pool_allocations),
                Decimal("0"),
            ) != self.distributed_cluster_points:
                raise ValueError("Category distributed totals do not match task total")
            if sum(
                (item.undistributed_cluster_points for item in self.category_pool_allocations),
                Decimal("0"),
            ) != self.undistributed_cluster_points:
                raise ValueError("Category undistributed totals do not match task total")
            pools = {item.category: item for item in self.category_pool_allocations}
            if any(
                allocation.category not in pools
                for allocation in self.cluster_allocations
            ):
                raise ValueError("Cluster category is missing its isolated pool")
            if any(
                allocation.parent_pool_type != FindingParentPoolType.CATEGORY_POOL
                or allocation.parent_pool_id
                != pools[allocation.category].parent_pool_id
                or allocation.parent_pool_points
                != pools[allocation.category].allocated_pool_points
                for allocation in self.cluster_allocations
            ):
                raise ValueError(
                    "Category cluster allocations must use their isolated category pool"
                )
        if self.outcome == TaskFindingCalculationOutcome.NO_ELIGIBLE_FINDINGS:
            if self.eligible_cluster_count != 0 or self.distributed_cluster_points != 0:
                raise ValueError("No-eligible-findings outcome has invalid totals")
        elif self.outcome == TaskFindingCalculationOutcome.NO_POSITIVE_FINDING_SCORE:
            if self.positive_score_cluster_count != 0 or self.distributed_cluster_points != 0:
                raise ValueError("No-positive-score outcome has invalid totals")
        elif self.outcome == TaskFindingCalculationOutcome.ALLOCATED:
            if self.positive_score_cluster_count == 0 or self.undistributed_cluster_points != 0:
                raise ValueError("Allocated outcome requires complete positive allocation")
        elif self.positive_score_cluster_count == 0 or self.undistributed_cluster_points == 0:
            raise ValueError("Partial outcome requires positive and undistributed points")
        if self.status == TaskFindingCalculationStatus.SUPERSEDED:
            if self.superseded_by_calculation_id is None:
                raise ValueError("Superseded calculations require a replacement")
        elif self.superseded_by_calculation_id is not None:
            raise ValueError("Only superseded calculations name a replacement")
        return self


class TaskFindingRewardCalculationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    processing_status: TaskFindingCalculationProcessingStatus
    calculation: TaskFindingRewardCalculation
    message: str = Field(..., min_length=1)


class TaskFindingRewardCalculationListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    routing_id: str
    total: int = Field(..., ge=0)
    calculations: list[TaskFindingRewardCalculation]
