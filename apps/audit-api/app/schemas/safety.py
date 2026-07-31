from enum import Enum

from pydantic import BaseModel, Field


class SafetyCheckStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    WARNING = "warning"


class SafetyIssueSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    HIGH = "high"
    CRITICAL = "critical"


class SafetyIssue(BaseModel):
    code: str
    severity: SafetyIssueSeverity
    message: str
    file_path: str | None = None
    line_number: int | None = None


class SafetyPreflightRequest(BaseModel):
    repo_path: str
    poc_file: str | None = None
    test_name: str | None = None
    command: list[str] | None = None


class SafetyPreflightResult(BaseModel):
    passed: bool
    status: SafetyCheckStatus
    issues: list[SafetyIssue]
    notes: list[str] = Field(default_factory=list)

