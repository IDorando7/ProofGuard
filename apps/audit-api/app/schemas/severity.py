from enum import Enum

from pydantic import BaseModel, Field


class NormalizedSeverity(str, Enum):
    CRITICAL = "Critical"
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"
    INFORMATIONAL = "Informational"


class SeverityChangeType(str, Enum):
    UNCHANGED = "unchanged"
    UPGRADED = "upgraded"
    DOWNGRADED = "downgraded"


class SeveritySignal(BaseModel):
    code: str
    weight: float = Field(..., ge=-1.0, le=1.0)
    message: str
    matched_text: str | None = None


class SeverityNormalizationResult(BaseModel):
    original_severity: NormalizedSeverity
    normalized_severity: NormalizedSeverity
    change_type: SeverityChangeType
    score: float = Field(..., ge=0.0, le=1.0)
    reason: str
    signals: list[SeveritySignal] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
