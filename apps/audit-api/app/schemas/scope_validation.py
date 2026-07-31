from enum import Enum

from pydantic import BaseModel, Field


class ScopeValidationStatus(str, Enum):
    IN_SCOPE = "in_scope"
    OUT_OF_SCOPE = "out_of_scope"
    INVALID_SCOPE = "invalid_scope"
    UNKNOWN = "unknown"


class ScopeIssueSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    HIGH = "high"
    CRITICAL = "critical"


class ScopeValidationIssue(BaseModel):
    code: str
    severity: ScopeIssueSeverity
    message: str
    field: str | None = None
    value: str | None = None


class ScopeValidationResult(BaseModel):
    passed: bool
    status: ScopeValidationStatus
    issues: list[ScopeValidationIssue]
    contracts_checked: list[str] = Field(default_factory=list)
    categories_checked: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

