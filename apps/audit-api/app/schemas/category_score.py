import math
import re
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.finding import FindingCategory
from app.schemas.node import normalize_category


CATEGORY_SCORE_VERSION = "category_score_v0"
CATEGORY_SCORE_SCORING_VERSION = CATEGORY_SCORE_VERSION
CATEGORY_SCORE_NEUTRAL_BASELINE = 0.50
CATEGORY_SCORE_TOTAL_PENALTY_CAP = 0.60
CATEGORY_SCORE_WEIGHT_TOLERANCE = 1e-9
SAFE_NODE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
SAFE_SCORE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,255}$")


class CategoryScoreBand(str, Enum):
    INSUFFICIENT_DATA = "insufficient_data"
    WEAK = "weak"
    DEVELOPING = "developing"
    STRONG = "strong"
    EXPERT = "expert"


class CategoryScoreProcessingStatus(str, Enum):
    CALCULATED = "calculated"
    UNCHANGED = "unchanged"


class CategoryScoreComponents(BaseModel):
    model_config = ConfigDict(extra="forbid")

    precision: float = Field(..., ge=0, le=1)
    reproduction_rate: float = Field(..., ge=0, le=1)
    uniqueness_rate: float = Field(..., ge=0, le=1)
    contribution_quality: float = Field(..., ge=0, le=1)
    consistency: float = Field(..., ge=0, le=1)
    experience_confidence: float = Field(..., ge=0, le=1)


class CategoryScoreWeights(BaseModel):
    model_config = ConfigDict(extra="forbid")

    precision_weight: float = Field(default=0.30, ge=0, le=1)
    reproduction_weight: float = Field(default=0.20, ge=0, le=1)
    uniqueness_weight: float = Field(default=0.15, ge=0, le=1)
    contribution_quality_weight: float = Field(default=0.20, ge=0, le=1)
    consistency_weight: float = Field(default=0.10, ge=0, le=1)
    experience_weight: float = Field(default=0.05, ge=0, le=1)

    @model_validator(mode="after")
    def validate_weight_total(self) -> "CategoryScoreWeights":
        positive_total = math.fsum(
            weight for weight in self.model_dump().values() if weight > 0
        )
        if not math.isclose(
            positive_total,
            1.0,
            rel_tol=0,
            abs_tol=CATEGORY_SCORE_WEIGHT_TOLERANCE,
        ):
            raise ValueError("Positive category-score weights must sum to 1.0")
        return self


class CategoryScoreWeightedComponents(BaseModel):
    model_config = ConfigDict(extra="forbid")

    precision_contribution: float = Field(..., ge=0, le=1)
    reproduction_contribution: float = Field(..., ge=0, le=1)
    uniqueness_contribution: float = Field(..., ge=0, le=1)
    contribution_quality_contribution: float = Field(..., ge=0, le=1)
    consistency_contribution: float = Field(..., ge=0, le=1)
    experience_contribution: float = Field(..., ge=0, le=1)
    positive_total: float = Field(..., ge=0, le=1)

    @model_validator(mode="after")
    def validate_positive_total(self) -> "CategoryScoreWeightedComponents":
        expected = round(
            math.fsum(
                (
                    self.precision_contribution,
                    self.reproduction_contribution,
                    self.uniqueness_contribution,
                    self.contribution_quality_contribution,
                    self.consistency_contribution,
                    self.experience_contribution,
                )
            ),
            6,
        )
        if not math.isclose(
            self.positive_total,
            expected,
            rel_tol=0,
            abs_tol=1e-6,
        ):
            raise ValueError(
                "positive_total must equal the sum of weighted contributions"
            )
        return self


class CategoryScorePenalties(BaseModel):
    model_config = ConfigDict(extra="forbid")

    duplicate_penalty: float = Field(..., ge=0, le=1)
    out_of_scope_penalty: float = Field(..., ge=0, le=1)
    insufficient_evidence_penalty: float = Field(..., ge=0, le=1)
    rejected_penalty: float = Field(..., ge=0, le=1)
    unsafe_penalty: float = Field(..., ge=0, le=1)
    unsupported_penalty: float = Field(..., ge=0, le=1)
    total_penalty: float = Field(
        ...,
        ge=0,
        le=CATEGORY_SCORE_TOTAL_PENALTY_CAP,
    )

    @model_validator(mode="after")
    def validate_total_penalty(self) -> "CategoryScorePenalties":
        expected = round(
            min(
                math.fsum(
                    (
                        self.duplicate_penalty,
                        self.out_of_scope_penalty,
                        self.insufficient_evidence_penalty,
                        self.rejected_penalty,
                        self.unsafe_penalty,
                        self.unsupported_penalty,
                    )
                ),
                CATEGORY_SCORE_TOTAL_PENALTY_CAP,
            ),
            6,
        )
        if not math.isclose(
            self.total_penalty,
            expected,
            rel_tol=0,
            abs_tol=1e-6,
        ):
            raise ValueError(
                "total_penalty must equal the capped sum of component penalties"
            )
        return self


class CategoryScoreRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score_id: str = Field(..., min_length=1, max_length=256)
    scoring_version: Literal["category_score_v0"]
    node_id: str = Field(..., min_length=1, max_length=128)
    category: FindingCategory
    performance_id: str = Field(..., min_length=1)
    performance_source_fingerprint: str = Field(
        ...,
        pattern=r"^[0-9a-f]{64}$",
    )
    components: CategoryScoreComponents
    weights: CategoryScoreWeights
    weighted_components: CategoryScoreWeightedComponents
    penalties: CategoryScorePenalties
    raw_score_before_penalties: float = Field(..., ge=0, le=1)
    raw_score_after_penalties: float = Field(..., ge=0, le=1)
    neutral_baseline: float = Field(..., ge=0, le=1)
    confidence_adjusted_score: float = Field(..., ge=0, le=1)
    category_score: float = Field(..., ge=0, le=1)
    score_band: CategoryScoreBand
    finalized_submissions: int = Field(..., ge=0)
    accepted_unique_submissions: int = Field(..., ge=0)
    source_fingerprint: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    explanation: list[str] = Field(..., min_length=1)
    calculated_at: datetime
    created_at: datetime
    updated_at: datetime

    @field_validator("node_id")
    @classmethod
    def validate_node_id(cls, value: str) -> str:
        if not SAFE_NODE_ID.fullmatch(value) or value in {".", ".."}:
            raise ValueError("Invalid node identifier")
        return value

    @field_validator("score_id")
    @classmethod
    def validate_score_id(cls, value: str) -> str:
        if not SAFE_SCORE_ID.fullmatch(value) or value in {".", ".."}:
            raise ValueError("Invalid category-score identifier")
        return value

    @field_validator("category", mode="before")
    @classmethod
    def normalize_record_category(cls, value: Any) -> str:
        return normalize_category(value)

    @field_validator("explanation")
    @classmethod
    def validate_explanation(cls, value: list[str]) -> list[str]:
        cleaned = [
            entry.strip() if isinstance(entry, str) else entry
            for entry in value
        ]
        if any(not isinstance(entry, str) or not entry for entry in cleaned):
            raise ValueError("Category-score explanations must not be empty")
        return cleaned

    @field_validator("calculated_at", "created_at", "updated_at")
    @classmethod
    def require_timezone_aware_datetime(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Category-score timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_record_relationships(self) -> "CategoryScoreRecord":
        expected_score_id = f"category_score_{self.node_id}_{self.category.value}"
        if self.score_id != expected_score_id:
            raise ValueError("Score identifier must match node and category")
        expected_performance_id = (
            f"performance_{self.node_id}_{self.category.value}"
        )
        if self.performance_id != expected_performance_id:
            raise ValueError(
                "Performance identifier must match score node and category"
            )
        if self.weights != CategoryScoreWeights():
            raise ValueError(
                "Category Score v0 records must use the fixed v0 weights"
            )
        if not math.isclose(
            self.neutral_baseline,
            CATEGORY_SCORE_NEUTRAL_BASELINE,
            rel_tol=0,
            abs_tol=1e-9,
        ):
            raise ValueError(
                "Category Score v0 records must use neutral baseline 0.50"
            )
        if self.accepted_unique_submissions > self.finalized_submissions:
            raise ValueError(
                "Accepted unique submissions cannot exceed finalized submissions"
            )
        if not math.isclose(
            self.raw_score_before_penalties,
            self.weighted_components.positive_total,
            rel_tol=0,
            abs_tol=1e-6,
        ):
            raise ValueError(
                "Raw score before penalties must equal positive weighted total"
            )
        if not math.isclose(
            self.category_score,
            self.confidence_adjusted_score,
            rel_tol=0,
            abs_tol=1e-6,
        ):
            raise ValueError(
                "category_score must equal the confidence-adjusted score"
            )
        expected_raw_after = round(
            max(
                0.0,
                min(
                    1.0,
                    self.raw_score_before_penalties
                    - self.penalties.total_penalty,
                ),
            ),
            6,
        )
        if not math.isclose(
            self.raw_score_after_penalties,
            expected_raw_after,
            rel_tol=0,
            abs_tol=1e-6,
        ):
            raise ValueError(
                "Raw score after penalties does not match stored components"
            )
        expected_adjusted = round(
            max(
                0.0,
                min(
                    1.0,
                    self.neutral_baseline
                    + self.components.experience_confidence
                    * (
                        self.raw_score_after_penalties
                        - self.neutral_baseline
                    ),
                ),
            ),
            6,
        )
        if not math.isclose(
            self.confidence_adjusted_score,
            expected_adjusted,
            rel_tol=0,
            abs_tol=1e-6,
        ):
            raise ValueError(
                "Confidence-adjusted score does not match stored inputs"
            )
        if self.components.experience_confidence < 0.30:
            expected_band = CategoryScoreBand.INSUFFICIENT_DATA
        elif self.category_score < 0.40:
            expected_band = CategoryScoreBand.WEAK
        elif self.category_score < 0.60:
            expected_band = CategoryScoreBand.DEVELOPING
        elif self.category_score < 0.80:
            expected_band = CategoryScoreBand.STRONG
        else:
            expected_band = CategoryScoreBand.EXPERT
        if self.score_band != expected_band:
            raise ValueError(
                "Score band does not match score and experience confidence"
            )
        return self


class CategoryScoreRebuildResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(..., min_length=1)
    category: str = Field(..., min_length=1)
    status: CategoryScoreProcessingStatus
    previous_source_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    current_source_fingerprint: str = Field(
        ...,
        pattern=r"^[0-9a-f]{64}$",
    )
    record: CategoryScoreRecord

    @field_validator("category", mode="before")
    @classmethod
    def normalize_result_category(cls, value: Any) -> str:
        return normalize_category(value)

    @model_validator(mode="after")
    def validate_result_identity(self) -> "CategoryScoreRebuildResult":
        if (
            self.node_id != self.record.node_id
            or self.category != self.record.category.value
        ):
            raise ValueError("Rebuild result identity must match its score record")
        if self.current_source_fingerprint != self.record.source_fingerprint:
            raise ValueError(
                "Current source fingerprint must match its score record"
            )
        return self


class CategoryScoreBatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_records: int = Field(..., ge=0)
    calculated_records: int = Field(..., ge=0)
    unchanged_records: int = Field(..., ge=0)
    failed_records: int = Field(..., ge=0)
    results: list[CategoryScoreRebuildResult]
    errors: list[str]

    @field_validator("errors")
    @classmethod
    def validate_errors(cls, value: list[str]) -> list[str]:
        cleaned = [
            error.strip() if isinstance(error, str) else error
            for error in value
        ]
        if any(not isinstance(error, str) or not error for error in cleaned):
            raise ValueError("Batch errors must not be empty")
        return cleaned

    @model_validator(mode="after")
    def validate_batch_counts(self) -> "CategoryScoreBatchResult":
        if self.calculated_records + self.unchanged_records != len(self.results):
            raise ValueError(
                "Successful batch counters must match the result count"
            )
        if len(self.errors) != self.failed_records:
            raise ValueError("Failed batch count must match the error count")
        if (
            self.calculated_records
            + self.unchanged_records
            + self.failed_records
            != self.total_records
        ):
            raise ValueError("Batch counters must sum to total_records")
        return self


class CategoryScoreRebuildRequest(BaseModel):
    """Empty request body that rejects client-controlled score inputs."""

    model_config = ConfigDict(extra="forbid")
