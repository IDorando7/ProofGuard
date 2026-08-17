from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, model_validator


class ValidationStatus(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"
    NEEDS_REVIEW = "needs_review"
    OUT_OF_SCOPE = "out_of_scope"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    UNSAFE_POC = "unsafe_poc"
    UNSUPPORTED = "unsupported"


class DuplicateKind(str, Enum):
    """The reason a duplicate relation exists.

    Submission retries are rejected by the submission protocol and therefore
    never appear as validation duplicate relations.
    """

    INDEPENDENT_ROOT_CAUSE = "independent_root_cause"


class ValidationEvidenceKey(str, Enum):
    HAS_FINDING = "has_finding"
    HAS_REPRODUCTION = "has_reproduction"
    REPRODUCTION_STATUS = "reproduction_status"
    HAS_POC_FILE = "has_poc_file"
    HAS_STDOUT = "has_stdout"
    HAS_STDERR = "has_stderr"
    IN_SCOPE = "in_scope"
    IS_DUPLICATE = "is_duplicate"
    NORMALIZED_SEVERITY = "normalized_severity"


class ValidationEvidence(BaseModel):
    has_finding: bool = False
    has_reproduction: bool = False
    reproduction_status: str | None = None
    has_poc_file: bool = False
    has_stdout: bool = False
    has_stderr: bool = False
    in_scope: bool | None = None
    is_duplicate: bool | None = None
    duplicate_of: str | None = None
    duplicate_kind: DuplicateKind | None = None
    is_valid_duplicate: bool | None = None
    canonical_finding_id: str | None = None
    original_severity: str | None = None
    normalized_severity: str | None = None
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_duplicate_semantics(self) -> "ValidationEvidence":
        if self.duplicate_kind == DuplicateKind.INDEPENDENT_ROOT_CAUSE:
            if self.is_duplicate is not True or self.is_valid_duplicate is not True:
                raise ValueError(
                    "Independent root-cause duplicates must be valid duplicate relations"
                )
            if not self.duplicate_of or not self.canonical_finding_id:
                raise ValueError(
                    "Independent root-cause duplicates require a canonical finding"
                )
            if self.duplicate_of != self.canonical_finding_id:
                raise ValueError("duplicate_of must match canonical_finding_id")
        elif self.is_valid_duplicate is True:
            raise ValueError("Valid duplicate evidence requires an explicit duplicate kind")
        return self


class ValidationDecisionCreate(BaseModel):
    project_id: str = Field(..., min_length=1)
    finding_id: str = Field(..., min_length=1)
    status: ValidationStatus = ValidationStatus.NEEDS_REVIEW
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = Field(..., min_length=1)
    evidence: ValidationEvidence = Field(default_factory=ValidationEvidence)
    validator_name: str = "validator_v0"


class ValidationDecision(ValidationDecisionCreate):
    validation_id: str
    created_at: datetime
    updated_at: datetime


class ValidationDecisionUpdate(BaseModel):
    status: ValidationStatus | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    reason: str | None = Field(default=None, min_length=1)
    evidence: ValidationEvidence | None = None
    validator_name: str | None = None
