from enum import Enum

from pydantic import BaseModel, Field, model_validator


class DeduplicationStatus(str, Enum):
    UNIQUE = "unique"
    DUPLICATE = "duplicate"
    POSSIBLE_DUPLICATE = "possible_duplicate"
    UNKNOWN = "unknown"


class DeduplicationCandidate(BaseModel):
    finding_id: str
    category: str | None = None
    contracts: list[str] = Field(default_factory=list)
    functions: list[str] = Field(default_factory=list)
    root_cause: str | None = None
    attack_path: str | None = None
    impact: str | None = None
    title: str | None = None
    severity: str | None = None


class DeduplicationMatch(BaseModel):
    finding_id: str
    similarity_score: float = Field(..., ge=0.0, le=1.0)
    status: DeduplicationStatus
    reason: str
    matched_fields: list[str] = Field(default_factory=list)


class DeduplicationResult(BaseModel):
    status: DeduplicationStatus
    is_duplicate: bool
    duplicate_of: str | None = None
    similarity_score: float = Field(..., ge=0.0, le=1.0)
    reason: str
    matches: list[DeduplicationMatch] = Field(default_factory=list)
    dedup_key: str | None = None
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_duplicate_consistency(self):
        if self.is_duplicate and self.status != DeduplicationStatus.DUPLICATE:
            raise ValueError("is_duplicate can only be true when status is duplicate")
        return self

