from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


REPORT_QUALITY_SCHEMA_VERSION = "report_quality_assessment_v1"
REPORT_QUALITY_POLICY_VERSION = "report_quality_policy_v1"
REPORT_QUALITY_CONFIGURATION_VERSION = "report_quality_config_v1"
REPORT_QUALITY_QUANTUM = Decimal("0.000001")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
ASSESSMENT_ID = re.compile(r"^report_quality_assessment_[0-9a-f]{32}$")


class ReportQualityAssessmentStatus(str, Enum):
    DRAFT = "draft"
    FINALIZED = "finalized"
    SUPERSEDED = "superseded"


class ReportQualityAssessmentMethod(str, Enum):
    DETERMINISTIC = "deterministic"
    VALIDATOR_SUPPLIED = "validator_supplied"
    HYBRID = "hybrid"


class QualityComponentName(str, Enum):
    CORRECTNESS = "correctness"
    POC_QUALITY = "poc_quality"
    ROOT_CAUSE_QUALITY = "root_cause_quality"
    IMPACT_QUALITY = "impact_quality"
    FIX_QUALITY = "fix_quality"


class QualityComponentState(str, Enum):
    ASSESSED = "assessed"
    NOT_ASSESSED = "not_assessed"
    NOT_APPLICABLE = "not_applicable"


class QualityAssessmentSource(str, Enum):
    VALIDATION_DECISION = "validation_decision"
    REPRODUCTION_RESULT = "reproduction_result"
    SEVERITY_NORMALIZATION = "severity_normalization"
    FINDING_STRUCTURED_FIELDS = "finding_structured_fields"
    VALIDATOR_ASSESSMENT = "validator_assessment"
    DETERMINISTIC_HEURISTIC = "deterministic_heuristic"
    NOT_ASSESSED = "not_assessed"


def _decimal_input(value: Any, label: str) -> Decimal:
    if isinstance(value, float):
        raise ValueError(f"{label} must be supplied as a Decimal or decimal string")
    try:
        decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a valid decimal") from exc
    if not decimal_value.is_finite():
        raise ValueError(f"{label} must be finite")
    return decimal_value


def _score_input(value: Any, label: str) -> Decimal:
    score = _decimal_input(value, label)
    if score < 0 or score > 1:
        raise ValueError(f"{label} must be between 0 and 1")
    if score != score.quantize(REPORT_QUALITY_QUANTUM):
        raise ValueError(f"{label} must use at most six decimal places")
    return score


def _sorted_unique(values: list[str], label: str) -> list[str]:
    cleaned = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} values must be non-empty strings")
        item = value.strip()
        if item.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", item):
            raise ValueError(f"{label} must not contain absolute host paths")
        cleaned.append(item)
    return sorted(set(cleaned))


class ReportQualityConfig(BaseModel):
    """Central Week 7 quality weights; malformed totals are never normalized."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    correctness: Decimal = Decimal("0.35")
    poc_quality: Decimal = Decimal("0.25")
    root_cause: Decimal = Decimal("0.20")
    impact: Decimal = Decimal("0.10")
    fix: Decimal = Decimal("0.10")

    @field_validator(
        "correctness", "poc_quality", "root_cause", "impact", "fix", mode="before"
    )
    @classmethod
    def validate_weight_input(cls, value: Any, info) -> Decimal:
        return _score_input(value, f"{info.field_name} weight")

    @model_validator(mode="after")
    def validate_exact_total(self) -> "ReportQualityConfig":
        if (
            self.correctness
            + self.poc_quality
            + self.root_cause
            + self.impact
            + self.fix
            != Decimal("1")
        ):
            raise ValueError("Report quality weights must sum exactly to 1")
        return self


class ReportQualityComponents(BaseModel):
    """The five normalized inputs accepted by the pure quality calculator."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    correctness_score: Decimal
    poc_quality_score: Decimal
    root_cause_quality_score: Decimal
    impact_quality_score: Decimal
    fix_quality_score: Decimal

    @field_validator("*", mode="before")
    @classmethod
    def validate_scores(cls, value: Any, info) -> Decimal:
        return _score_input(value, info.field_name)


class QualityComponentAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    component_name: QualityComponentName
    score: Decimal | None = None
    weight: Decimal
    weighted_value: Decimal | None = None
    assessment_source: QualityAssessmentSource
    reason_codes: list[str] = Field(default_factory=list)
    evidence_references: list[str] = Field(default_factory=list)
    state: QualityComponentState
    assessment_policy_version: str = Field(..., min_length=1, max_length=128)

    @field_validator("score", mode="before")
    @classmethod
    def validate_optional_score(cls, value: Any, info) -> Decimal | None:
        return None if value is None else _score_input(value, info.field_name)

    @field_validator("weight", mode="before")
    @classmethod
    def validate_weight(cls, value: Any) -> Decimal:
        return _score_input(value, "component weight")

    @field_validator("weighted_value", mode="before")
    @classmethod
    def validate_weighted_value(cls, value: Any) -> Decimal | None:
        if value is None:
            return None
        result = _decimal_input(value, "weighted_value")
        if result < 0 or result > 1:
            raise ValueError("weighted_value must be between 0 and 1")
        return result

    @field_validator("reason_codes")
    @classmethod
    def validate_reasons(cls, value: list[str]) -> list[str]:
        return _sorted_unique(value, "Reason code")

    @field_validator("evidence_references")
    @classmethod
    def validate_evidence(cls, value: list[str]) -> list[str]:
        return _sorted_unique(value, "Evidence reference")

    @model_validator(mode="after")
    def validate_completeness(self) -> "QualityComponentAssessment":
        if self.state == QualityComponentState.ASSESSED:
            if self.score is None or self.weighted_value is None:
                raise ValueError("Assessed quality components require score and weighted value")
            if self.weighted_value != (self.score * self.weight).quantize(
                REPORT_QUALITY_QUANTUM
            ):
                raise ValueError("weighted_value must equal score multiplied by weight")
            if self.assessment_source == QualityAssessmentSource.NOT_ASSESSED:
                raise ValueError("Assessed components require an assessment source")
        else:
            if self.score is not None or self.weighted_value is not None:
                raise ValueError("Unassessed components cannot contain a score")
            if self.assessment_source != QualityAssessmentSource.NOT_ASSESSED:
                raise ValueError("Unassessed components must use the not_assessed source")
        return self


class ReportQualityComponentAssessments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    correctness: QualityComponentAssessment
    poc_quality: QualityComponentAssessment
    root_cause_quality: QualityComponentAssessment
    impact_quality: QualityComponentAssessment
    fix_quality: QualityComponentAssessment

    @model_validator(mode="after")
    def validate_names(self) -> "ReportQualityComponentAssessments":
        expected = {
            "correctness": QualityComponentName.CORRECTNESS,
            "poc_quality": QualityComponentName.POC_QUALITY,
            "root_cause_quality": QualityComponentName.ROOT_CAUSE_QUALITY,
            "impact_quality": QualityComponentName.IMPACT_QUALITY,
            "fix_quality": QualityComponentName.FIX_QUALITY,
        }
        for field_name, component_name in expected.items():
            if getattr(self, field_name).component_name != component_name:
                raise ValueError(f"{field_name} contains the wrong component name")
        return self

    def ordered(self) -> list[QualityComponentAssessment]:
        return [
            self.correctness,
            self.poc_quality,
            self.root_cause_quality,
            self.impact_quality,
            self.fix_quality,
        ]

    def complete(self) -> bool:
        return all(item.state == QualityComponentState.ASSESSED for item in self.ordered())

    def scores(self) -> ReportQualityComponents | None:
        if not self.complete():
            return None
        return ReportQualityComponents(
            correctness_score=self.correctness.score,
            poc_quality_score=self.poc_quality.score,
            root_cause_quality_score=self.root_cause_quality.score,
            impact_quality_score=self.impact_quality.score,
            fix_quality_score=self.fix_quality.score,
        )


class ReportQualityAssessmentRequest(BaseModel):
    """Validator/protocol input. Identity, total Q, and reward fields are server-derived."""

    model_config = ConfigDict(extra="forbid")

    correctness_score: Decimal | None = None
    poc_quality_score: Decimal | None = None
    root_cause_quality_score: Decimal | None = None
    impact_quality_score: Decimal | None = None
    fix_quality_score: Decimal | None = None
    reason_codes: dict[QualityComponentName, list[str]] = Field(default_factory=dict)
    evidence_references: dict[QualityComponentName, list[str]] = Field(default_factory=dict)
    finalize: bool = True
    supersede_existing: bool = False

    @field_validator(
        "correctness_score",
        "poc_quality_score",
        "root_cause_quality_score",
        "impact_quality_score",
        "fix_quality_score",
        mode="before",
    )
    @classmethod
    def validate_optional_scores(cls, value: Any, info) -> Decimal | None:
        return None if value is None else _score_input(value, info.field_name)

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_map(
        cls, value: dict[QualityComponentName, list[str]]
    ) -> dict[QualityComponentName, list[str]]:
        return {key: _sorted_unique(items, "Reason code") for key, items in value.items()}

    @field_validator("evidence_references")
    @classmethod
    def validate_evidence_map(
        cls, value: dict[QualityComponentName, list[str]]
    ) -> dict[QualityComponentName, list[str]]:
        return {
            key: _sorted_unique(items, "Evidence reference")
            for key, items in value.items()
        }


class ReportQualityAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    report_quality_assessment_id: str
    schema_version: Literal["report_quality_assessment_v1"] = REPORT_QUALITY_SCHEMA_VERSION
    assessment_version: int = Field(..., ge=1)
    assessment_policy_version: str = Field(..., min_length=1, max_length=128)
    configuration_version: str = Field(..., min_length=1, max_length=128)
    project_id: str = Field(..., min_length=1, max_length=256)
    routing_id: str = Field(..., min_length=1, max_length=256)
    finding_cluster_id: str = Field(..., min_length=1, max_length=256)
    submission_id: str = Field(..., min_length=1, max_length=256)
    finding_id: str = Field(..., min_length=1, max_length=256)
    node_id: str = Field(..., min_length=1, max_length=256)
    operator_id: str = Field(..., min_length=1, max_length=128)
    submitted_at: datetime
    validation_id: str = Field(..., min_length=1, max_length=256)
    reproduction_id: str | None = Field(default=None, min_length=1, max_length=256)
    final_severity: str = Field(..., min_length=1, max_length=32)
    components: ReportQualityComponentAssessments
    quality_score: Decimal | None = None
    assessment_status: ReportQualityAssessmentStatus
    assessment_method: ReportQualityAssessmentMethod
    cluster_source_fingerprint: str
    member_source_fingerprint: str
    validation_source_fingerprint: str
    reproduction_source_fingerprint: str | None = None
    severity_source_fingerprint: str
    finding_source_fingerprint: str
    source_fingerprint: str
    supersedes_assessment_id: str | None = None
    superseded_by_assessment_id: str | None = None
    created_at: datetime
    updated_at: datetime
    finalized_at: datetime | None = None

    @field_validator("report_quality_assessment_id", "supersedes_assessment_id", "superseded_by_assessment_id")
    @classmethod
    def validate_assessment_ids(cls, value: str | None) -> str | None:
        if value is not None and not ASSESSMENT_ID.fullmatch(value):
            raise ValueError("Invalid report quality assessment identifier")
        return value

    @field_validator(
        "cluster_source_fingerprint",
        "member_source_fingerprint",
        "validation_source_fingerprint",
        "reproduction_source_fingerprint",
        "severity_source_fingerprint",
        "finding_source_fingerprint",
        "source_fingerprint",
    )
    @classmethod
    def validate_fingerprints(cls, value: str | None) -> str | None:
        if value is not None and not SHA256_HEX.fullmatch(value):
            raise ValueError("Fingerprint must be a lowercase SHA-256 digest")
        return value

    @field_validator("quality_score", mode="before")
    @classmethod
    def validate_quality_score(cls, value: Any) -> Decimal | None:
        return None if value is None else _score_input(value, "quality_score")

    @field_validator("submitted_at", "created_at", "updated_at", "finalized_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Report quality timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "ReportQualityAssessment":
        complete = self.components.complete()
        weights = sum((item.weight for item in self.components.ordered()), Decimal("0"))
        if weights != Decimal("1"):
            raise ValueError("Persisted report quality component weights must sum to 1")
        if complete:
            reconstructed = sum(
                (item.score * item.weight for item in self.components.ordered()),
                Decimal("0"),
            ).quantize(REPORT_QUALITY_QUANTUM)
            if self.quality_score != reconstructed:
                raise ValueError("quality_score must match the weighted component total")
        if self.assessment_status in {
            ReportQualityAssessmentStatus.FINALIZED,
            ReportQualityAssessmentStatus.SUPERSEDED,
        }:
            if not complete or self.quality_score is None or self.finalized_at is None:
                raise ValueError("Finalized quality assessments require all five components")
        elif self.finalized_at is not None:
            raise ValueError("Draft quality assessments cannot have finalized_at")
        if not complete and self.quality_score is not None:
            raise ValueError("Incomplete quality assessments cannot contain final Q")
        if self.assessment_status == ReportQualityAssessmentStatus.SUPERSEDED:
            if self.superseded_by_assessment_id is None:
                raise ValueError("Superseded assessments require their replacement ID")
        elif self.superseded_by_assessment_id is not None:
            raise ValueError("Only superseded assessments identify a replacement")
        return self


class ReportQualityAssessmentListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    routing_id: str
    finding_cluster_id: str
    total: int = Field(..., ge=0)
    assessments: list[ReportQualityAssessment]


class ReportQualityRebuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    supersede_changed_finalized: bool = False


class ReportQualityRebuildResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    routing_id: str
    total_assessments: int = Field(..., ge=0)
    created_assessments: int = Field(..., ge=0)
    updated_assessments: int = Field(..., ge=0)
    unchanged_assessments: int = Field(..., ge=0)
    draft_assessments: int = Field(..., ge=0)
    finalized_assessments: int = Field(..., ge=0)
    assessments: list[ReportQualityAssessment]

    @model_validator(mode="after")
    def validate_counts(self) -> "ReportQualityRebuildResult":
        if self.total_assessments != len(self.assessments):
            raise ValueError("Quality rebuild total must match returned assessments")
        if (
            self.created_assessments
            + self.updated_assessments
            + self.unchanged_assessments
            != self.total_assessments
        ):
            raise ValueError("Quality rebuild operation counts must match total")
        if self.draft_assessments + self.finalized_assessments != self.total_assessments:
            raise ValueError("Quality rebuild lifecycle counts must match total")
        return self
