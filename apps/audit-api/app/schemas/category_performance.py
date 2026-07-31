import re
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.finding import FindingCategory
from app.schemas.node import normalize_category


CATEGORY_PERFORMANCE_VERSION = "category_performance_v0"
SAFE_NODE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
SAFE_PERFORMANCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,255}$")


class CategoryPerformanceOutcome(str, Enum):
    ACCEPTED_UNIQUE = "accepted_unique"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"
    OUT_OF_SCOPE = "out_of_scope"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    UNSAFE = "unsafe"
    UNSUPPORTED = "unsupported"


class CategoryPerformanceCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_finalized_submissions: int = Field(default=0, ge=0)
    accepted_unique_submissions: int = Field(default=0, ge=0)
    rejected_submissions: int = Field(default=0, ge=0)
    duplicate_submissions: int = Field(default=0, ge=0)
    out_of_scope_submissions: int = Field(default=0, ge=0)
    insufficient_evidence_submissions: int = Field(default=0, ge=0)
    unsafe_submissions: int = Field(default=0, ge=0)
    unsupported_submissions: int = Field(default=0, ge=0)
    reproduced_submissions: int = Field(default=0, ge=0)
    reproduction_attempts: int = Field(default=0, ge=0)
    reward_eligible_submissions: int = Field(default=0, ge=0)
    rewarded_submissions: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_counter_relationships(self) -> "CategoryPerformanceCounts":
        total = self.total_finalized_submissions
        primary_outcomes = (
            self.accepted_unique_submissions,
            self.rejected_submissions,
            self.duplicate_submissions,
            self.out_of_scope_submissions,
            self.insufficient_evidence_submissions,
            self.unsafe_submissions,
            self.unsupported_submissions,
        )
        if any(value > total for value in primary_outcomes):
            raise ValueError("Outcome counters cannot exceed total finalized submissions")
        if sum(primary_outcomes) > total:
            raise ValueError("A finalized submission cannot occupy multiple primary outcome buckets")
        if self.reproduction_attempts > total:
            raise ValueError("Reproduction attempts cannot exceed total finalized submissions")
        if self.reproduced_submissions > self.reproduction_attempts:
            raise ValueError("Reproduced submissions cannot exceed reproduction attempts")
        if self.reward_eligible_submissions > total:
            raise ValueError("Reward-eligible submissions cannot exceed total finalized submissions")
        if self.rewarded_submissions > self.reward_eligible_submissions:
            raise ValueError("Rewarded submissions cannot exceed reward-eligible submissions")
        return self


class CategoryPerformanceContributionStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_contribution_score: float = Field(default=0, ge=0)
    average_contribution_score: float = Field(default=0, ge=0, le=100)
    minimum_contribution_score: float | None = Field(default=None, ge=0, le=100)
    maximum_contribution_score: float | None = Field(default=None, ge=0, le=100)
    accepted_contribution_score_total: float = Field(default=0, ge=0)
    accepted_average_contribution_score: float = Field(default=0, ge=0, le=100)
    accepted_minimum_contribution_score: float | None = Field(
        default=None,
        ge=0,
        le=100,
    )
    accepted_maximum_contribution_score: float | None = Field(
        default=None,
        ge=0,
        le=100,
    )

    @model_validator(mode="after")
    def validate_score_relationships(self) -> "CategoryPerformanceContributionStats":
        if (self.minimum_contribution_score is None) != (
            self.maximum_contribution_score is None
        ):
            raise ValueError("Minimum and maximum contribution scores must both be present or absent")
        if (
            self.minimum_contribution_score is not None
            and self.maximum_contribution_score is not None
            and self.minimum_contribution_score > self.maximum_contribution_score
        ):
            raise ValueError("Minimum contribution score cannot exceed maximum contribution score")
        if self.minimum_contribution_score is None:
            if self.total_contribution_score != 0 or self.average_contribution_score != 0:
                raise ValueError("Score totals and averages require score-bearing source events")
        if self.accepted_contribution_score_total > self.total_contribution_score:
            raise ValueError("Accepted contribution total cannot exceed total contribution score")
        if (self.accepted_minimum_contribution_score is None) != (
            self.accepted_maximum_contribution_score is None
        ):
            raise ValueError(
                "Accepted minimum and maximum contribution scores must both be present or absent"
            )
        if (
            self.accepted_minimum_contribution_score is not None
            and self.accepted_maximum_contribution_score is not None
            and self.accepted_minimum_contribution_score
            > self.accepted_maximum_contribution_score
        ):
            raise ValueError(
                "Accepted minimum contribution score cannot exceed accepted maximum"
            )
        return self


class CategoryPerformanceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    performance_id: str = Field(..., min_length=1, max_length=256)
    performance_version: Literal["category_performance_v0"]
    node_id: str = Field(..., min_length=1, max_length=128)
    category: FindingCategory
    counts: CategoryPerformanceCounts
    contribution_stats: CategoryPerformanceContributionStats
    source_event_ids: list[str]
    source_submission_ids: list[str]
    source_fingerprint: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    first_activity_at: datetime | None
    last_activity_at: datetime | None
    rebuilt_at: datetime
    created_at: datetime
    updated_at: datetime

    @field_validator("node_id")
    @classmethod
    def validate_node_id(cls, value: str) -> str:
        if not SAFE_NODE_ID.fullmatch(value) or value in {".", ".."}:
            raise ValueError("Invalid node identifier")
        return value

    @field_validator("performance_id")
    @classmethod
    def validate_performance_id(cls, value: str) -> str:
        if not SAFE_PERFORMANCE_ID.fullmatch(value) or value in {".", ".."}:
            raise ValueError("Invalid category-performance identifier")
        return value

    @field_validator("category", mode="before")
    @classmethod
    def normalize_record_category(cls, value: Any) -> str:
        return normalize_category(value)

    @field_validator("source_event_ids", "source_submission_ids")
    @classmethod
    def validate_deterministic_source_ids(cls, value: list[str]) -> list[str]:
        if any(not isinstance(item, str) or not item.strip() for item in value):
            raise ValueError("Source identifiers must not be empty")
        if len(value) != len(set(value)):
            raise ValueError("Source identifiers must be unique")
        if value != sorted(value):
            raise ValueError("Source identifiers must use deterministic ascending order")
        return value

    @field_validator(
        "first_activity_at",
        "last_activity_at",
        "rebuilt_at",
        "created_at",
        "updated_at",
    )
    @classmethod
    def require_timezone_aware_datetime(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Category-performance timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_record_relationships(self) -> "CategoryPerformanceRecord":
        expected_id = f"performance_{self.node_id}_{self.category.value}"
        if self.performance_id != expected_id:
            raise ValueError("Performance identifier must match node and category")
        if len(self.source_event_ids) != len(self.source_submission_ids):
            raise ValueError("Source event and submission lists must have equal lengths")
        if self.counts.total_finalized_submissions != len(self.source_event_ids):
            raise ValueError("Finalized count must match the number of source events")
        if (
            self.first_activity_at is None
            or self.last_activity_at is None
        ) and self.counts.total_finalized_submissions != 0:
            raise ValueError("Non-empty performance records require activity timestamps")
        if self.counts.total_finalized_submissions == 0 and (
            self.first_activity_at is not None or self.last_activity_at is not None
        ):
            raise ValueError("Empty performance records cannot have activity timestamps")
        if (
            self.first_activity_at is not None
            and self.last_activity_at is not None
            and self.first_activity_at > self.last_activity_at
        ):
            raise ValueError("First activity cannot occur after last activity")
        primary_outcome_total = sum(
            (
                self.counts.accepted_unique_submissions,
                self.counts.rejected_submissions,
                self.counts.duplicate_submissions,
                self.counts.out_of_scope_submissions,
                self.counts.insufficient_evidence_submissions,
                self.counts.unsafe_submissions,
                self.counts.unsupported_submissions,
            )
        )
        if primary_outcome_total != self.counts.total_finalized_submissions:
            raise ValueError("Every finalized submission requires exactly one primary outcome")
        finalized = self.counts.total_finalized_submissions
        stats = self.contribution_stats
        if finalized == 0:
            if stats != CategoryPerformanceContributionStats():
                raise ValueError("Empty performance records require zero contribution statistics")
        else:
            if (
                stats.minimum_contribution_score is None
                or stats.maximum_contribution_score is None
            ):
                raise ValueError("Non-empty performance records require contribution score bounds")
            if round(stats.total_contribution_score / finalized, 6) != round(
                stats.average_contribution_score, 6
            ):
                raise ValueError("Average contribution score does not match finalized sources")
            if not (
                stats.minimum_contribution_score
                <= stats.average_contribution_score
                <= stats.maximum_contribution_score
            ):
                raise ValueError("Average contribution score must be within score bounds")
        accepted = self.counts.accepted_unique_submissions
        if accepted == 0:
            if (
                stats.accepted_contribution_score_total != 0
                or stats.accepted_average_contribution_score != 0
                or stats.accepted_minimum_contribution_score is not None
                or stats.accepted_maximum_contribution_score is not None
            ):
                raise ValueError("No accepted sources require zero accepted score statistics")
        else:
            if round(stats.accepted_contribution_score_total / accepted, 6) != round(
                stats.accepted_average_contribution_score, 6
            ):
                raise ValueError("Accepted average score does not match accepted sources")
            if (
                stats.accepted_minimum_contribution_score is not None
                and stats.accepted_maximum_contribution_score is not None
                and not (
                    stats.accepted_minimum_contribution_score
                    <= stats.accepted_average_contribution_score
                    <= stats.accepted_maximum_contribution_score
                )
            ):
                raise ValueError(
                    "Accepted average contribution score must be within accepted bounds"
                )
        return self


class CategoryPerformanceListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int = Field(..., ge=0)
    records: list[CategoryPerformanceRecord]


class NodeCategoryPerformanceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(..., min_length=1)
    total_categories: int = Field(..., ge=0)
    records: list[CategoryPerformanceRecord]


class CategoryPerformanceRebuildRequest(BaseModel):
    """Empty body that rejects client-controlled aggregate values."""

    model_config = ConfigDict(extra="forbid")
