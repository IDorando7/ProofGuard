from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class ValidationStatus(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"
    NEEDS_REVIEW = "needs_review"
    OUT_OF_SCOPE = "out_of_scope"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    UNSAFE_POC = "unsafe_poc"
    UNSUPPORTED = "unsupported"


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
    original_severity: str | None = None
    normalized_severity: str | None = None
    notes: list[str] = Field(default_factory=list)


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

