from __future__ import annotations

import re
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.finding import FindingCategory
from app.schemas.node import normalize_category
from app.schemas.validator_attestation import ValidatorAssignmentRole


VALIDATION_QUALITY_POLICY_VERSION = "validation_quality_policy_v1"
VALIDATION_QUALITY_ASSESSMENT_VERSION = "validation_quality_assessment_v1"
VALIDATOR_PERFORMANCE_EVENT_VERSION = "validator_performance_event_v1"
VALIDATOR_PROTOCOL_VIOLATION_EVENT_VERSION = "validator_protocol_violation_event_v1"
VALIDATOR_CATEGORY_PERFORMANCE_VERSION = "validator_category_performance_v1"
VALIDATOR_CATEGORY_SCORE_POLICY_VERSION = "validator_category_score_policy_v1"
VALIDATOR_CATEGORY_SCORE_VERSION = "validator_category_score_v1"
VALIDATOR_MEMBERSHIP_POLICY_VERSION = "validator_membership_policy_v1"
VALIDATOR_MEMBERSHIP_VERSION = "validator_membership_v1"
VALIDATOR_SCORE_QUANTUM = Decimal("0.000001")
NEUTRAL_VALIDATOR_SCORE = Decimal("0.500000")
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,255}$")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def decimal_input(value: Any, label: str) -> Decimal:
    if isinstance(value, float):
        raise ValueError(f"{label} must use Decimal or a decimal string")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a valid Decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{label} must be finite")
    return result


def score_decimal(value: Any, label: str) -> Decimal:
    result = decimal_input(value, label)
    if result < 0 or result > 1:
        raise ValueError(f"{label} must be between zero and one")
    if result != result.quantize(VALIDATOR_SCORE_QUANTUM):
        raise ValueError(f"{label} must use at most six decimal places")
    return result


def quantize_score(value: Decimal) -> Decimal:
    return min(Decimal("1"), max(Decimal("0"), value)).quantize(
        VALIDATOR_SCORE_QUANTUM, rounding=ROUND_HALF_EVEN
    )


def _identifier(value: str) -> str:
    if (
        not isinstance(value, str)
        or not SAFE_IDENTIFIER.fullmatch(value)
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
    ):
        raise ValueError("Invalid validator-performance identifier")
    return value


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("Validator-performance timestamps must be timezone-aware")
    return value


class ValidationQualityComponent(str, Enum):
    VALIDITY = "validity"
    REPRODUCTION = "reproduction"
    ROOT_CAUSE = "root_cause"
    SEVERITY = "severity"
    IMPACT = "impact"
    PROTOCOL_COMPLIANCE = "protocol_compliance"


QUALITY_COMPONENT_ORDER = list(ValidationQualityComponent)


class ValidationQualityAssessmentStatus(str, Enum):
    CALCULATED = "calculated"
    FINALIZED = "finalized"


class ValidatorProtocolViolationCode(str, Enum):
    CONFLICT_OF_INTEREST_BREACH = "conflict_of_interest_breach"
    FORGED_REPRODUCTION_LINK = "forged_reproduction_link"
    UNAUTHORIZED_DUPLICATE_ATTESTATION = "unauthorized_duplicate_attestation"
    SAFETY_BYPASS_ATTEMPT = "safety_bypass_attempt"


class ValidatorProtocolViolationEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    validator_protocol_violation_event_id: str
    event_version: Literal["validator_protocol_violation_event_v1"] = (
        VALIDATOR_PROTOCOL_VIOLATION_EVENT_VERSION
    )
    project_id: str
    routing_id: str
    finding_cluster_id: str
    validator_assignment_id: str
    validator_node_id: str
    validator_operator_id: str
    violation_code: ValidatorProtocolViolationCode
    adjudication_reference: str
    source_fingerprint: str
    adjudicated_at: datetime

    @field_validator(
        "validator_protocol_violation_event_id", "project_id", "routing_id",
        "finding_cluster_id", "validator_assignment_id", "validator_node_id",
        "validator_operator_id", "adjudication_reference",
    )
    @classmethod
    def ids(cls, value):
        return _identifier(value)

    @field_validator("source_fingerprint")
    @classmethod
    def fingerprint(cls, value):
        if not SHA256_HEX.fullmatch(value):
            raise ValueError("Protocol violation event fingerprint must be SHA-256")
        return value

    @field_validator("adjudicated_at")
    @classmethod
    def time(cls, value):
        return _aware(value)


class ValidationQualityPolicyV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_version: Literal["validation_quality_policy_v1"] = (
        VALIDATION_QUALITY_POLICY_VERSION
    )
    validity_weight: Decimal = Decimal("0.35")
    reproduction_weight: Decimal = Decimal("0.20")
    root_cause_weight: Decimal = Decimal("0.15")
    severity_weight: Decimal = Decimal("0.15")
    impact_weight: Decimal = Decimal("0.10")
    protocol_compliance_weight: Decimal = Decimal("0.05")
    quantum: Decimal = VALIDATOR_SCORE_QUANTUM

    @field_validator(
        "validity_weight", "reproduction_weight", "root_cause_weight",
        "severity_weight", "impact_weight", "protocol_compliance_weight",
        mode="before",
    )
    @classmethod
    def weights(cls, value, info):
        return score_decimal(value, info.field_name)

    @field_validator("quantum", mode="before")
    @classmethod
    def exact_quantum(cls, value):
        result = decimal_input(value, "quantum")
        if result != VALIDATOR_SCORE_QUANTUM:
            raise ValueError("Validation-quality quantum must be 0.000001")
        return result

    @model_validator(mode="after")
    def exact_total(self) -> "ValidationQualityPolicyV1":
        if sum(self.weight_map().values(), Decimal("0")) != Decimal("1"):
            raise ValueError("Validation-quality weights must sum exactly to 1.00")
        return self

    def weight_map(self) -> dict[ValidationQualityComponent, Decimal]:
        return {
            ValidationQualityComponent.VALIDITY: self.validity_weight,
            ValidationQualityComponent.REPRODUCTION: self.reproduction_weight,
            ValidationQualityComponent.ROOT_CAUSE: self.root_cause_weight,
            ValidationQualityComponent.SEVERITY: self.severity_weight,
            ValidationQualityComponent.IMPACT: self.impact_weight,
            ValidationQualityComponent.PROTOCOL_COMPLIANCE: self.protocol_compliance_weight,
        }


class ValidationQualityAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    validation_quality_assessment_id: str
    assessment_version: Literal["validation_quality_assessment_v1"] = (
        VALIDATION_QUALITY_ASSESSMENT_VERSION
    )
    quality_policy: ValidationQualityPolicyV1
    project_id: str
    routing_id: str
    finding_cluster_id: str
    final_validation_consensus_id: str
    validation_round_id: str
    validator_committee_id: str
    validator_assignment_id: str
    validator_node_id: str
    validator_operator_id: str
    assignment_role: ValidatorAssignmentRole
    category: FindingCategory
    attestation_id: str
    validator_reproduction_id: str
    validity_accuracy: Decimal
    reproduction_accuracy: Decimal | None = None
    root_cause_accuracy: Decimal | None = None
    severity_accuracy: Decimal | None = None
    impact_accuracy: Decimal | None = None
    protocol_compliance: Decimal
    applicable_components: list[ValidationQualityComponent]
    validation_quality_score: Decimal
    source_fingerprint: str
    status: ValidationQualityAssessmentStatus
    created_at: datetime
    calculated_at: datetime
    finalized_at: datetime | None = None

    @field_validator(
        "validation_quality_assessment_id", "project_id", "routing_id",
        "finding_cluster_id", "final_validation_consensus_id", "validation_round_id",
        "validator_committee_id", "validator_assignment_id", "validator_node_id",
        "validator_operator_id", "attestation_id", "validator_reproduction_id",
    )
    @classmethod
    def ids(cls, value):
        return _identifier(value)

    @field_validator("category", mode="before")
    @classmethod
    def category_value(cls, value):
        return normalize_category(value)

    @field_validator(
        "validity_accuracy", "reproduction_accuracy", "root_cause_accuracy",
        "severity_accuracy", "impact_accuracy", "protocol_compliance",
        "validation_quality_score", mode="before",
    )
    @classmethod
    def scores(cls, value, info):
        return None if value is None else score_decimal(value, info.field_name)

    @field_validator("applicable_components", mode="before")
    @classmethod
    def components(cls, value):
        selected = {ValidationQualityComponent(item) for item in value}
        return [item for item in QUALITY_COMPONENT_ORDER if item in selected]

    @field_validator("source_fingerprint")
    @classmethod
    def fingerprint(cls, value):
        if not SHA256_HEX.fullmatch(value):
            raise ValueError("Assessment fingerprint must be SHA-256")
        return value

    @field_validator("created_at", "calculated_at", "finalized_at")
    @classmethod
    def timestamps(cls, value):
        return _aware(value)

    @model_validator(mode="after")
    def validate_assessment(self) -> "ValidationQualityAssessment":
        score_by_component = {
            ValidationQualityComponent.VALIDITY: self.validity_accuracy,
            ValidationQualityComponent.REPRODUCTION: self.reproduction_accuracy,
            ValidationQualityComponent.ROOT_CAUSE: self.root_cause_accuracy,
            ValidationQualityComponent.SEVERITY: self.severity_accuracy,
            ValidationQualityComponent.IMPACT: self.impact_accuracy,
            ValidationQualityComponent.PROTOCOL_COMPLIANCE: self.protocol_compliance,
        }
        actual = [item for item in QUALITY_COMPONENT_ORDER if score_by_component[item] is not None]
        if self.applicable_components != actual:
            raise ValueError("Applicable components must exactly match scored components")
        weights = self.quality_policy.weight_map()
        denominator = sum((weights[item] for item in actual), Decimal("0"))
        expected = quantize_score(
            sum((weights[item] * score_by_component[item] for item in actual), Decimal("0"))
            / denominator
        )
        if self.validation_quality_score != expected:
            raise ValueError("Validation quality score does not match applicable weights")
        if self.status == ValidationQualityAssessmentStatus.FINALIZED:
            if self.finalized_at is None:
                raise ValueError("Finalized assessment requires finalized_at")
        elif self.finalized_at is not None:
            raise ValueError("Calculated assessment cannot set finalized_at")
        return self


class ValidatorPerformanceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    validator_performance_event_id: str
    event_version: Literal["validator_performance_event_v1"] = (
        VALIDATOR_PERFORMANCE_EVENT_VERSION
    )
    validator_node_id: str
    operator_id: str
    project_id: str
    routing_id: str
    finding_cluster_id: str
    category: FindingCategory
    assignment_role: ValidatorAssignmentRole
    validation_quality_assessment_id: str
    final_validation_consensus_id: str
    validity_correct: bool
    reproduction_correct: bool | None = None
    root_cause_correct: bool | None = None
    severity_correct: bool | None = None
    impact_correct: bool | None = None
    protocol_compliant: bool
    validation_quality_score: Decimal
    finalized_at: datetime
    source_fingerprint: str

    @field_validator(
        "validator_performance_event_id", "validator_node_id", "operator_id",
        "project_id", "routing_id", "finding_cluster_id",
        "validation_quality_assessment_id", "final_validation_consensus_id",
    )
    @classmethod
    def ids(cls, value):
        return _identifier(value)

    @field_validator("category", mode="before")
    @classmethod
    def category_value(cls, value):
        return normalize_category(value)

    @field_validator("validation_quality_score", mode="before")
    @classmethod
    def score(cls, value):
        return score_decimal(value, "validation_quality_score")

    @field_validator("finalized_at")
    @classmethod
    def time(cls, value):
        return _aware(value)

    @field_validator("source_fingerprint")
    @classmethod
    def fingerprint(cls, value):
        if not SHA256_HEX.fullmatch(value):
            raise ValueError("Performance event fingerprint must be SHA-256")
        return value


class ValidatorCategoryPerformance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    validator_category_performance_id: str
    performance_version: Literal["validator_category_performance_v1"] = (
        VALIDATOR_CATEGORY_PERFORMANCE_VERSION
    )
    validator_node_id: str
    operator_id: str
    category: FindingCategory
    resolved_validations: int = Field(..., ge=0)
    authoritative_resolved_validations: int = Field(..., ge=0)
    shadow_resolved_validations: int = Field(..., ge=0)
    validity_evaluated: int = Field(..., ge=0)
    validity_correct: int = Field(..., ge=0)
    reproduction_evaluated: int = Field(..., ge=0)
    reproduction_correct: int = Field(..., ge=0)
    root_cause_evaluated: int = Field(..., ge=0)
    root_cause_correct: int = Field(..., ge=0)
    severity_evaluated: int = Field(..., ge=0)
    severity_correct: int = Field(..., ge=0)
    impact_evaluated: int = Field(..., ge=0)
    impact_correct: int = Field(..., ge=0)
    protocol_compliance_evaluated: int = Field(..., ge=0)
    protocol_compliant_count: int = Field(..., ge=0)
    protocol_violation_count: int = Field(..., ge=0)
    completed_assignments: int = Field(..., ge=0)
    quality_score_sum: Decimal
    quality_score_count: int = Field(..., ge=0)
    average_quality_score: Decimal
    source_event_ids: list[str]
    source_fingerprint: str
    first_resolved_validation_at: datetime | None = None
    last_resolved_validation_at: datetime | None = None
    rebuilt_at: datetime
    created_at: datetime

    @field_validator("validator_category_performance_id", "validator_node_id", "operator_id")
    @classmethod
    def ids(cls, value):
        return _identifier(value)

    @field_validator("category", mode="before")
    @classmethod
    def category_value(cls, value):
        return normalize_category(value)

    @field_validator("quality_score_sum", mode="before")
    @classmethod
    def score_sum(cls, value):
        result = decimal_input(value, "quality_score_sum")
        if result < 0 or result != result.quantize(VALIDATOR_SCORE_QUANTUM):
            raise ValueError("Quality score sum must be non-negative with six decimals")
        return result

    @field_validator("average_quality_score", mode="before")
    @classmethod
    def average(cls, value):
        return score_decimal(value, "average_quality_score")

    @field_validator("source_event_ids", mode="before")
    @classmethod
    def event_ids(cls, value):
        values = sorted({_identifier(item) for item in value})
        return values

    @field_validator("source_fingerprint")
    @classmethod
    def fingerprint(cls, value):
        if not SHA256_HEX.fullmatch(value):
            raise ValueError("Performance fingerprint must be SHA-256")
        return value

    @field_validator(
        "first_resolved_validation_at", "last_resolved_validation_at",
        "rebuilt_at", "created_at",
    )
    @classmethod
    def times(cls, value):
        return _aware(value)

    @model_validator(mode="after")
    def counters(self) -> "ValidatorCategoryPerformance":
        if self.resolved_validations != len(self.source_event_ids):
            raise ValueError("Resolved validations must match source events")
        if self.authoritative_resolved_validations + self.shadow_resolved_validations != self.resolved_validations:
            raise ValueError("Role counters must partition resolved validations")
        for correct, evaluated in (
            (self.validity_correct, self.validity_evaluated),
            (self.reproduction_correct, self.reproduction_evaluated),
            (self.root_cause_correct, self.root_cause_evaluated),
            (self.severity_correct, self.severity_evaluated),
            (self.impact_correct, self.impact_evaluated),
            (self.protocol_compliant_count, self.protocol_compliance_evaluated),
        ):
            if correct > evaluated:
                raise ValueError("Correct component count cannot exceed evaluated count")
        if self.protocol_compliant_count + self.protocol_violation_count != self.protocol_compliance_evaluated:
            raise ValueError("Compliance counters must partition evaluated work")
        if self.quality_score_count != self.resolved_validations or self.completed_assignments != self.resolved_validations:
            raise ValueError("V1 completed and quality counts must match resolved events")
        expected_average = (
            quantize_score(self.quality_score_sum / self.quality_score_count)
            if self.quality_score_count
            else Decimal("0.000000")
        )
        if self.average_quality_score != expected_average:
            raise ValueError("Average quality score does not match event sum")
        return self


class ValidatorCategoryScorePolicyV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_version: Literal["validator_category_score_policy_v1"] = (
        VALIDATOR_CATEGORY_SCORE_POLICY_VERSION
    )
    validity_weight: Decimal = Decimal("0.30")
    reproduction_weight: Decimal = Decimal("0.20")
    root_cause_weight: Decimal = Decimal("0.15")
    severity_weight: Decimal = Decimal("0.15")
    impact_weight: Decimal = Decimal("0.10")
    protocol_compliance_weight: Decimal = Decimal("0.05")
    assignment_reliability_weight: Decimal = Decimal("0.05")
    neutral_default: Decimal = NEUTRAL_VALIDATOR_SCORE
    full_confidence_resolved_validations: Literal[10] = 10

    @field_validator(
        "validity_weight", "reproduction_weight", "root_cause_weight",
        "severity_weight", "impact_weight", "protocol_compliance_weight",
        "assignment_reliability_weight", mode="before",
    )
    @classmethod
    def weights(cls, value, info):
        return score_decimal(value, info.field_name)

    @field_validator("neutral_default", mode="before")
    @classmethod
    def exact_neutral(cls, value):
        result = score_decimal(value, "neutral_default")
        if result != NEUTRAL_VALIDATOR_SCORE:
            raise ValueError("Validator neutral default must be 0.500000")
        return result

    @model_validator(mode="after")
    def exact_total(self) -> "ValidatorCategoryScorePolicyV1":
        total = sum(
            (
                self.validity_weight, self.reproduction_weight,
                self.root_cause_weight, self.severity_weight,
                self.impact_weight, self.protocol_compliance_weight,
                self.assignment_reliability_weight,
            ),
            Decimal("0"),
        )
        if total != Decimal("1"):
            raise ValueError("Validator category-score weights must sum exactly to 1.00")
        return self


class ValidatorCategoryScore(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    validator_category_score_id: str
    score_version: Literal["validator_category_score_v1"] = VALIDATOR_CATEGORY_SCORE_VERSION
    score_policy: ValidatorCategoryScorePolicyV1
    validator_node_id: str
    operator_id: str
    category: FindingCategory
    validator_category_performance_id: str
    performance_source_fingerprint: str
    resolved_validations: int = Field(..., ge=0)
    validity_accuracy: Decimal
    reproduction_accuracy: Decimal
    root_cause_accuracy: Decimal
    severity_accuracy: Decimal
    impact_accuracy: Decimal
    protocol_compliance: Decimal
    assignment_reliability: Decimal
    raw_score: Decimal
    experience_confidence: Decimal
    final_score: Decimal
    source_fingerprint: str
    calculated_at: datetime
    created_at: datetime

    @field_validator("validator_category_score_id", "validator_node_id", "operator_id", "validator_category_performance_id")
    @classmethod
    def ids(cls, value):
        return _identifier(value)

    @field_validator("category", mode="before")
    @classmethod
    def category_value(cls, value):
        return normalize_category(value)

    @field_validator(
        "validity_accuracy", "reproduction_accuracy", "root_cause_accuracy",
        "severity_accuracy", "impact_accuracy", "protocol_compliance",
        "assignment_reliability", "raw_score", "experience_confidence",
        "final_score", mode="before",
    )
    @classmethod
    def scores(cls, value, info):
        return score_decimal(value, info.field_name)

    @field_validator("performance_source_fingerprint", "source_fingerprint")
    @classmethod
    def fingerprints(cls, value):
        if not SHA256_HEX.fullmatch(value):
            raise ValueError("Validator score fingerprint must be SHA-256")
        return value

    @field_validator("calculated_at", "created_at")
    @classmethod
    def times(cls, value):
        return _aware(value)

    @model_validator(mode="after")
    def validate_score_formula(self) -> "ValidatorCategoryScore":
        policy = self.score_policy
        expected_raw = quantize_score(
            policy.validity_weight * self.validity_accuracy
            + policy.reproduction_weight * self.reproduction_accuracy
            + policy.root_cause_weight * self.root_cause_accuracy
            + policy.severity_weight * self.severity_accuracy
            + policy.impact_weight * self.impact_accuracy
            + policy.protocol_compliance_weight * self.protocol_compliance
            + policy.assignment_reliability_weight * self.assignment_reliability
        )
        expected_confidence = quantize_score(
            min(
                Decimal("1"),
                Decimal(self.resolved_validations)
                / Decimal(policy.full_confidence_resolved_validations),
            )
        )
        expected_final = quantize_score(
            policy.neutral_default
            + expected_confidence * (expected_raw - policy.neutral_default)
        )
        if self.raw_score != expected_raw:
            raise ValueError("Raw validator category score does not match components")
        if self.experience_confidence != expected_confidence:
            raise ValueError("Experience confidence does not match resolved history")
        if self.final_score != expected_final:
            raise ValueError("Final validator category score does not match shrinkage")
        return self


class ValidatorMembershipStatus(str, Enum):
    CANDIDATE = "candidate"
    PROBATION = "probation"
    ACTIVE = "active"
    EXPERT = "expert"
    SUSPENDED = "suspended"
    REMOVED = "removed"


class ValidatorMembershipPolicyV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_version: Literal["validator_membership_policy_v1"] = (
        VALIDATOR_MEMBERSHIP_POLICY_VERSION
    )
    probation_minimum_resolved: Literal[3] = 3
    probation_minimum_score: Decimal = Decimal("0.400000")
    active_minimum_resolved: Literal[5] = 5
    active_minimum_score: Decimal = Decimal("0.600000")
    active_retention_score: Decimal = Decimal("0.500000")
    active_minimum_validity_samples: Literal[3] = 3
    expert_minimum_resolved: Literal[10] = 10
    expert_minimum_score: Decimal = Decimal("0.800000")
    expert_retention_score: Decimal = Decimal("0.700000")
    expert_minimum_validity_accuracy: Decimal = Decimal("0.800000")
    expert_minimum_reproduction_accuracy: Decimal = Decimal("0.750000")
    expert_minimum_reproduction_samples: Literal[3] = 3

    @field_validator(
        "probation_minimum_score", "active_minimum_score", "active_retention_score",
        "expert_minimum_score", "expert_retention_score",
        "expert_minimum_validity_accuracy", "expert_minimum_reproduction_accuracy",
        mode="before",
    )
    @classmethod
    def scores(cls, value, info):
        return score_decimal(value, info.field_name)


class ValidatorMembership(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    validator_membership_id: str
    membership_version: Literal["validator_membership_v1"] = VALIDATOR_MEMBERSHIP_VERSION
    membership_policy: ValidatorMembershipPolicyV1
    validator_node_id: str
    operator_id: str
    category: FindingCategory
    validator_category_score_id: str
    score_source_fingerprint: str
    previous_status: ValidatorMembershipStatus | None = None
    status: ValidatorMembershipStatus
    reason_codes: list[str]
    source_fingerprint: str
    created_at: datetime
    evaluated_at: datetime

    @field_validator("validator_membership_id", "validator_node_id", "operator_id", "validator_category_score_id")
    @classmethod
    def ids(cls, value):
        return _identifier(value)

    @field_validator("category", mode="before")
    @classmethod
    def category_value(cls, value):
        return normalize_category(value)

    @field_validator("reason_codes", mode="before")
    @classmethod
    def reasons(cls, value):
        values = sorted({str(item).strip() for item in value})
        if any(not item for item in values):
            raise ValueError("Membership reasons cannot be empty")
        return values

    @field_validator("score_source_fingerprint", "source_fingerprint")
    @classmethod
    def fingerprints(cls, value):
        if not SHA256_HEX.fullmatch(value):
            raise ValueError("Validator membership fingerprint must be SHA-256")
        return value

    @field_validator("created_at", "evaluated_at")
    @classmethod
    def times(cls, value):
        return _aware(value)


class ValidatorPerformanceEvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ValidatorPerformanceEvaluationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    final_validation_consensus_id: str
    assessments: list[ValidationQualityAssessment]
    events: list[ValidatorPerformanceEvent]
    category_performance: list[ValidatorCategoryPerformance]
    category_scores: list[ValidatorCategoryScore]
    memberships: list[ValidatorMembership]


class ValidationQualityAssessmentListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int = Field(..., ge=0)
    assessments: list[ValidationQualityAssessment]


class ValidatorPerformanceStateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category_performance: ValidatorCategoryPerformance
    category_score: ValidatorCategoryScore
    membership: ValidatorMembership
