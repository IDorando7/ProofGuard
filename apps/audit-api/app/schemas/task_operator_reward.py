from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.finding import FindingCategory
from app.schemas.finding_cluster import FindingClusterMemberRelation
from app.schemas.node import normalize_category
from app.schemas.reward import RewardDomain, RewardUnit
from app.schemas.subnet_reward import MAX_PROTOCOL_POINTS, PROTOCOL_POINTS_QUANTUM


TASK_OPERATOR_CALCULATION_VERSION = "task_operator_reward_calculation_v1"
TASK_OPERATOR_POLICY_VERSION = "operator_cluster_payout_v1"
TASK_OPERATOR_CONFIGURATION_VERSION = "operator_cluster_payout_config_v1"
QUALITY_WEIGHT_QUANTUM = Decimal("0.000000000001")
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,255}$")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
CALCULATION_ID = re.compile(r"^task_operator_reward_calculation_[0-9a-f]{32}$")


class OperatorRewardExclusionReason(str, Enum):
    SUPERSEDED_BY_OPERATOR_BEST = "superseded_by_operator_best"
    OUTSIDE_TOP_K = "outside_top_k"
    MISSING_QUALITY_ASSESSMENT = "missing_quality_assessment"
    DRAFT_QUALITY_ASSESSMENT = "draft_quality_assessment"
    SUPERSEDED_QUALITY_ASSESSMENT = "superseded_quality_assessment"
    INVALID_CLUSTER_MEMBER = "invalid_cluster_member"
    UNAUTHORIZED_ASSIGNMENT = "unauthorized_assignment"
    OPERATOR_IDENTITY_MISMATCH = "operator_identity_mismatch"
    NO_POSITIVE_QUALITY_WEIGHT = "no_positive_quality_weight"
    ZERO_QUALITY_POOL = "zero_quality_pool"


class ClusterPayoutOutcome(str, Enum):
    ALLOCATED = "allocated"
    NO_ELIGIBLE_REPORTS = "no_eligible_reports"
    NO_POSITIVE_QUALITY_WEIGHT = "no_positive_quality_weight"


class TaskOperatorCalculationStatus(str, Enum):
    CALCULATED = "calculated"
    SUPERSEDED = "superseded"


class TaskOperatorProcessingStatus(str, Enum):
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


def _ratio(value: Any, label: str) -> Decimal:
    result = _decimal(value, label)
    if result < 0 or result > 1:
        raise ValueError(f"{label} must be between zero and one")
    if result != result.quantize(PROTOCOL_POINTS_QUANTUM):
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


class DuplicateRewardConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    top_k: int = Field(default=5, ge=1, le=1000)


class ChiefFinderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    bonus_percentage: Decimal = Decimal("0.05")
    quality_percentage: Decimal = Decimal("0.95")
    quality_threshold: Decimal = Decimal("0.80")

    @model_validator(mode="before")
    @classmethod
    def derive_legacy_quality_percentage(cls, value: Any) -> Any:
        if isinstance(value, dict) and "quality_percentage" not in value:
            migrated = dict(value)
            chief = _decimal(
                migrated.get("bonus_percentage", Decimal("0.05")),
                "bonus_percentage",
            )
            migrated["quality_percentage"] = Decimal("1") - chief
            return migrated
        return value

    @field_validator(
        "bonus_percentage", "quality_percentage", "quality_threshold", mode="before"
    )
    @classmethod
    def validate_ratios(cls, value: Any, info) -> Decimal:
        return _ratio(value, info.field_name)

    @model_validator(mode="after")
    def validate_pool_total(self) -> "ChiefFinderConfig":
        if self.bonus_percentage + self.quality_percentage != Decimal("1"):
            raise ValueError("Chief and quality pool shares must sum exactly to one")
        return self


class TaskOperatorRewardConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    duplicates: DuplicateRewardConfig = DuplicateRewardConfig()
    chief_finder: ChiefFinderConfig = ChiefFinderConfig()


class TaskOperatorRewardCalculationRequest(BaseModel):
    """Preview request; all rankings, identities, Q values, and rewards are derived."""

    model_config = ConfigDict(extra="forbid")

    task_finding_reward_calculation_id: str

    @field_validator("task_finding_reward_calculation_id")
    @classmethod
    def validate_day4_id(cls, value: str) -> str:
        return _identifier(value, "Day 4 calculation")


class EligibleReportRewardInput(BaseModel):
    """Pure, validated report input assembled by the orchestration service."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    finding_cluster_id: str
    submission_id: str
    finding_id: str
    node_id: str
    operator_id: str
    routing_assignment_id: str
    submitted_at: datetime
    member_relation: FindingClusterMemberRelation
    member_source_fingerprint: str
    assessment_id: str
    assessment_source_fingerprint: str
    quality_score: Decimal
    chief_root_cause_qualified: bool
    chief_severity_qualified: bool
    chief_impact_qualified: bool
    chief_evidence_reason_codes: list[str]
    chief_evidence_references: list[str]

    @field_validator(
        "finding_cluster_id",
        "submission_id",
        "finding_id",
        "node_id",
        "operator_id",
        "routing_assignment_id",
        "assessment_id",
    )
    @classmethod
    def validate_ids(cls, value: str, info) -> str:
        return _identifier(value, info.field_name)

    @field_validator("member_source_fingerprint", "assessment_source_fingerprint")
    @classmethod
    def validate_fingerprints(cls, value: str) -> str:
        if not SHA256_HEX.fullmatch(value):
            raise ValueError("Report reward fingerprints must be SHA-256")
        return value

    @field_validator("quality_score", mode="before")
    @classmethod
    def validate_quality(cls, value: Any) -> Decimal:
        return _ratio(value, "quality_score")

    @field_validator("submitted_at")
    @classmethod
    def validate_submitted_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Report submission timestamp must be timezone-aware")
        return value

    @field_validator("chief_evidence_reason_codes", "chief_evidence_references")
    @classmethod
    def sort_evidence(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(not item for item in cleaned):
            raise ValueError("Chief evidence values must not be empty")
        return sorted(set(cleaned))


class ReportRewardExclusion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    finding_cluster_id: str
    submission_id: str
    node_id: str
    operator_id: str | None = None
    quality_score: Decimal | None = None
    reason: OperatorRewardExclusionReason
    representative_submission_id: str | None = None
    quality_assessment_id: str | None = None

    @field_validator(
        "finding_cluster_id",
        "submission_id",
        "node_id",
        "operator_id",
        "representative_submission_id",
        "quality_assessment_id",
    )
    @classmethod
    def validate_ids(cls, value: str | None, info) -> str | None:
        return None if value is None else _identifier(value, info.field_name)

    @field_validator("quality_score", mode="before")
    @classmethod
    def validate_optional_quality(cls, value: Any) -> Decimal | None:
        return None if value is None else _ratio(value, "quality_score")


class OperatorClusterRewardAllocation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    finding_cluster_id: str
    operator_id: str
    rewarded_node_id: str
    rewarded_submission_id: str
    quality_assessment_id: str
    quality_score: Decimal
    quality_weight: Decimal
    quality_rank: int = Field(..., ge=1)
    top_k_eligible: bool
    rewarded: bool
    chief_finder: bool
    chief_qualifying_submission_id: str | None = None
    chief_qualifying_submitted_at: datetime | None = None
    quality_reward_points: Decimal
    chief_bonus_points: Decimal
    total_reward_points: Decimal
    exclusion_reason: OperatorRewardExclusionReason | None = None
    calculation_policy_version: str

    @field_validator(
        "finding_cluster_id",
        "operator_id",
        "rewarded_node_id",
        "rewarded_submission_id",
        "quality_assessment_id",
        "chief_qualifying_submission_id",
    )
    @classmethod
    def validate_ids(cls, value: str | None, info) -> str | None:
        return None if value is None else _identifier(value, info.field_name)

    @field_validator("quality_score", mode="before")
    @classmethod
    def validate_quality(cls, value: Any) -> Decimal:
        return _ratio(value, "quality_score")

    @field_validator("quality_weight", mode="before")
    @classmethod
    def validate_quality_weight(cls, value: Any) -> Decimal:
        weight = _decimal(value, "quality_weight")
        if weight < 0 or weight > 1:
            raise ValueError("quality_weight must be between zero and one")
        if weight != weight.quantize(QUALITY_WEIGHT_QUANTUM):
            raise ValueError("quality_weight must use at most twelve decimal places")
        return weight

    @field_validator(
        "quality_reward_points", "chief_bonus_points", "total_reward_points", mode="before"
    )
    @classmethod
    def validate_points(cls, value: Any, info) -> Decimal:
        return _points(value, info.field_name)

    @field_validator("chief_qualifying_submitted_at")
    @classmethod
    def validate_chief_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Chief qualifying timestamp must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_allocation(self) -> "OperatorClusterRewardAllocation":
        if self.quality_weight != self.quality_score * self.quality_score:
            raise ValueError("Quality weight must equal finalized Q squared")
        if self.total_reward_points != self.quality_reward_points + self.chief_bonus_points:
            raise ValueError("Operator total must equal quality reward plus Chief bonus")
        if self.chief_finder:
            if (
                not self.top_k_eligible
                or not self.rewarded
                or self.chief_qualifying_submission_id is None
            ):
                raise ValueError("Chief Finder must be Top-K and have qualifying evidence")
        elif self.chief_bonus_points != 0:
            raise ValueError("Only Chief Finder may receive the Chief bonus")
        if self.rewarded != (self.total_reward_points > 0):
            raise ValueError("Rewarded state must reflect positive task reward")
        if not self.top_k_eligible and self.exclusion_reason != OperatorRewardExclusionReason.OUTSIDE_TOP_K:
            raise ValueError("Operators outside Top-K require an exclusion reason")
        return self


class FindingClusterOperatorPayoutCalculation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    finding_cluster_id: str
    project_id: str
    routing_id: str
    category: FindingCategory
    finding_cluster_source_fingerprint: str
    day4_allocation_source_fingerprint: str
    cluster_reward_points: Decimal
    eligible_report_count: int = Field(..., ge=0)
    distinct_eligible_operator_count: int = Field(..., ge=0)
    top_k_limit: int = Field(..., ge=1)
    rewarded_operator_count: int = Field(..., ge=0)
    chief_bonus_percentage: Decimal
    quality_pool_percentage: Decimal
    chief_quality_threshold: Decimal
    chief_operator_id: str | None = None
    chief_qualifying_submission_id: str | None = None
    chief_bonus_pool_points: Decimal
    quality_pool_points: Decimal
    total_quality_weight: Decimal
    operator_allocations: list[OperatorClusterRewardAllocation]
    report_exclusions: list[ReportRewardExclusion]
    distributed_points: Decimal
    undistributed_points: Decimal
    outcome: ClusterPayoutOutcome
    policy_version: str
    source_fingerprint: str

    @model_validator(mode="before")
    @classmethod
    def derive_legacy_quality_pool_percentage(cls, value: Any) -> Any:
        if isinstance(value, dict) and "quality_pool_percentage" not in value:
            migrated = dict(value)
            chief = _decimal(
                migrated.get("chief_bonus_percentage", Decimal("0.05")),
                "chief_bonus_percentage",
            )
            migrated["quality_pool_percentage"] = Decimal("1") - chief
            return migrated
        return value

    @field_validator(
        "finding_cluster_id", "project_id", "routing_id", "chief_operator_id",
        "chief_qualifying_submission_id",
    )
    @classmethod
    def validate_ids(cls, value: str | None, info) -> str | None:
        return None if value is None else _identifier(value, info.field_name)

    @field_validator("category", mode="before")
    @classmethod
    def normalize_category(cls, value: Any) -> str:
        return normalize_category(value)

    @field_validator(
        "finding_cluster_source_fingerprint",
        "day4_allocation_source_fingerprint",
        "source_fingerprint",
    )
    @classmethod
    def validate_fingerprints(cls, value: str) -> str:
        if not SHA256_HEX.fullmatch(value):
            raise ValueError("Cluster payout fingerprint must be SHA-256")
        return value

    @field_validator(
        "chief_bonus_percentage",
        "quality_pool_percentage",
        "chief_quality_threshold",
        mode="before",
    )
    @classmethod
    def validate_ratios(cls, value: Any, info) -> Decimal:
        return _ratio(value, info.field_name)

    @field_validator(
        "cluster_reward_points", "chief_bonus_pool_points", "quality_pool_points",
        "distributed_points", "undistributed_points", mode="before",
    )
    @classmethod
    def validate_points(cls, value: Any, info) -> Decimal:
        return _points(value, info.field_name)

    @field_validator("total_quality_weight", mode="before")
    @classmethod
    def validate_total_weight(cls, value: Any) -> Decimal:
        result = _decimal(value, "total_quality_weight")
        if result < 0 or result != result.quantize(QUALITY_WEIGHT_QUANTUM):
            raise ValueError("Total quality weight must be non-negative at twelve decimals")
        return result

    @model_validator(mode="after")
    def validate_conservation(self) -> "FindingClusterOperatorPayoutCalculation":
        if self.chief_bonus_percentage + self.quality_pool_percentage != Decimal("1"):
            raise ValueError("Persisted Chief and quality shares must sum exactly to one")
        if self.chief_bonus_pool_points + self.quality_pool_points != self.cluster_reward_points:
            raise ValueError("Chief and quality pools must equal the Day 4 cluster reward")
        if self.distributed_points + self.undistributed_points != self.cluster_reward_points:
            raise ValueError("Cluster operator payout must conserve the Day 4 reward")
        if self.distributed_points != sum(
            (item.total_reward_points for item in self.operator_allocations), Decimal("0")
        ):
            raise ValueError("Distributed cluster points must match operator allocations")
        if self.rewarded_operator_count != sum(item.rewarded for item in self.operator_allocations):
            raise ValueError("Rewarded operator count does not match allocations")
        if self.rewarded_operator_count > self.top_k_limit:
            raise ValueError("Positive operator recipients cannot exceed Top-K")
        quality_distributed = sum(
            (item.quality_reward_points for item in self.operator_allocations),
            Decimal("0"),
        )
        if self.total_quality_weight > 0:
            if quality_distributed != self.quality_pool_points:
                raise ValueError("Positive Q squared weights must consume the Quality Pool")
        elif quality_distributed != 0:
            raise ValueError("Zero total quality weight cannot distribute Quality Pool")
        operator_ids = [item.operator_id for item in self.operator_allocations]
        if len(operator_ids) != len(set(operator_ids)):
            raise ValueError("One operator may occupy only one cluster allocation")
        if self.distinct_eligible_operator_count != len(self.operator_allocations):
            raise ValueError("Eligible operator count must match allocations")
        ranks = [item.quality_rank for item in self.operator_allocations]
        if ranks != list(range(1, len(ranks) + 1)):
            raise ValueError("Quality ranks must be deterministic ordinals")
        chiefs = [item for item in self.operator_allocations if item.chief_finder]
        if self.chief_operator_id is None:
            if chiefs or self.chief_bonus_pool_points != 0:
                raise ValueError("No-Chief cluster cannot contain Chief allocation")
        elif len(chiefs) != 1 or chiefs[0].operator_id != self.chief_operator_id:
            raise ValueError("Chief identity must match exactly one operator allocation")
        if self.report_exclusions != sorted(
            self.report_exclusions, key=lambda item: (item.submission_id, item.reason.value)
        ):
            raise ValueError("Report exclusions must be deterministically sorted")
        return self


class OperatorTaskRewardSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operator_id: str
    rewarded_cluster_count: int = Field(..., ge=0)
    rewarded_finding_cluster_ids: list[str]
    total_reward_points: Decimal

    @field_validator("operator_id")
    @classmethod
    def validate_operator(cls, value: str) -> str:
        return _identifier(value, "operator")

    @field_validator("rewarded_finding_cluster_ids")
    @classmethod
    def validate_cluster_ids(cls, value: list[str]) -> list[str]:
        cleaned = [_identifier(item, "finding cluster") for item in value]
        if cleaned != sorted(set(cleaned)):
            raise ValueError("Rewarded cluster identifiers must be unique and sorted")
        return cleaned

    @field_validator("total_reward_points", mode="before")
    @classmethod
    def validate_total(cls, value: Any) -> Decimal:
        return _points(value, "operator task reward total")

    @model_validator(mode="after")
    def validate_count(self) -> "OperatorTaskRewardSummary":
        if self.rewarded_cluster_count != len(self.rewarded_finding_cluster_ids):
            raise ValueError("Operator rewarded cluster count must match IDs")
        return self


class TaskOperatorRewardCalculation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    calculation_id: str
    calculation_version: Literal["task_operator_reward_calculation_v1"] = (
        TASK_OPERATOR_CALCULATION_VERSION
    )
    policy_version: str
    configuration_version: str
    status: TaskOperatorCalculationStatus
    project_id: str
    routing_id: str
    task_reward_budget_id: str
    day4_calculation_id: str
    day4_source_fingerprint: str
    reward_domain: Literal[RewardDomain.CLIENT_TASK] = RewardDomain.CLIENT_TASK
    reward_unit: Literal[RewardUnit.PROTOCOL_POINTS] = RewardUnit.PROTOCOL_POINTS
    source_cluster_reward_points: Decimal
    cluster_payouts: list[FindingClusterOperatorPayoutCalculation]
    operator_summaries: list[OperatorTaskRewardSummary]
    distributed_operator_points: Decimal
    undistributed_cluster_points: Decimal
    source_fingerprint: str
    supersedes_calculation_id: str | None = None
    superseded_by_calculation_id: str | None = None
    created_at: datetime
    calculated_at: datetime

    @field_validator("calculation_id", "supersedes_calculation_id", "superseded_by_calculation_id")
    @classmethod
    def validate_calculation_ids(cls, value: str | None) -> str | None:
        if value is not None and not CALCULATION_ID.fullmatch(value):
            raise ValueError("Invalid task operator calculation identifier")
        return value

    @field_validator("project_id", "routing_id", "task_reward_budget_id", "day4_calculation_id")
    @classmethod
    def validate_ids(cls, value: str, info) -> str:
        return _identifier(value, info.field_name)

    @field_validator("day4_source_fingerprint", "source_fingerprint")
    @classmethod
    def validate_fingerprints(cls, value: str) -> str:
        if not SHA256_HEX.fullmatch(value):
            raise ValueError("Task operator fingerprint must be SHA-256")
        return value

    @field_validator(
        "source_cluster_reward_points", "distributed_operator_points",
        "undistributed_cluster_points", mode="before",
    )
    @classmethod
    def validate_points(cls, value: Any, info) -> Decimal:
        return _points(value, info.field_name)

    @field_validator("created_at", "calculated_at")
    @classmethod
    def validate_timestamps(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Task operator timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_task_conservation(self) -> "TaskOperatorRewardCalculation":
        if self.cluster_payouts != sorted(
            self.cluster_payouts, key=lambda item: item.finding_cluster_id
        ):
            raise ValueError("Cluster payouts must be deterministically sorted")
        if self.operator_summaries != sorted(
            self.operator_summaries, key=lambda item: item.operator_id
        ):
            raise ValueError("Operator summaries must be deterministically sorted")
        if self.source_cluster_reward_points != sum(
            (item.cluster_reward_points for item in self.cluster_payouts), Decimal("0")
        ):
            raise ValueError("Task source total must equal Day 4 cluster rewards")
        if self.distributed_operator_points != sum(
            (item.distributed_points for item in self.cluster_payouts), Decimal("0")
        ):
            raise ValueError("Task distributed total must match cluster payouts")
        if self.undistributed_cluster_points != sum(
            (item.undistributed_points for item in self.cluster_payouts), Decimal("0")
        ):
            raise ValueError("Task undistributed total must match cluster payouts")
        if self.distributed_operator_points + self.undistributed_cluster_points != self.source_cluster_reward_points:
            raise ValueError("Task operator calculation must conserve Day 4 rewards")
        reconstructed: dict[str, list[OperatorClusterRewardAllocation]] = {}
        for cluster in self.cluster_payouts:
            for allocation in cluster.operator_allocations:
                if allocation.rewarded:
                    reconstructed.setdefault(allocation.operator_id, []).append(allocation)
        summaries = {item.operator_id: item for item in self.operator_summaries}
        if set(summaries) != set(reconstructed):
            raise ValueError("Operator summaries must cover exactly rewarded operators")
        for operator_id, allocations in reconstructed.items():
            summary = summaries[operator_id]
            if (
                summary.rewarded_finding_cluster_ids
                != sorted(item.finding_cluster_id for item in allocations)
                or summary.total_reward_points
                != sum((item.total_reward_points for item in allocations), Decimal("0"))
            ):
                raise ValueError("Operator summary does not match cluster allocations")
        if self.status == TaskOperatorCalculationStatus.SUPERSEDED:
            if self.superseded_by_calculation_id is None:
                raise ValueError("Superseded calculation requires its replacement")
        elif self.superseded_by_calculation_id is not None:
            raise ValueError("Only superseded calculation may name a replacement")
        return self


class TaskOperatorRewardCalculationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    processing_status: TaskOperatorProcessingStatus
    calculation: TaskOperatorRewardCalculation
    message: str


class TaskOperatorRewardCalculationListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    routing_id: str
    total: int = Field(..., ge=0)
    calculations: list[TaskOperatorRewardCalculation]
