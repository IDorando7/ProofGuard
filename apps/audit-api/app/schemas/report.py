from datetime import datetime

from pydantic import BaseModel, Field


class ReportFindingSummary(BaseModel):
    finding_id: str
    title: str | None = None
    category: str | None = None
    original_severity: str | None = None
    normalized_severity: str | None = None
    validation_status: str
    confidence: float | None = None
    contract: str | None = None
    function: str | None = None
    reproduction_status: str | None = None
    poc_file: str | None = None
    test_name: str | None = None
    reason: str | None = None


class SeverityBreakdown(BaseModel):
    Critical: int = 0
    High: int = 0
    Medium: int = 0
    Low: int = 0
    Informational: int = 0


class ValidationBreakdown(BaseModel):
    accepted: int = 0
    rejected: int = 0
    duplicate: int = 0
    needs_review: int = 0
    out_of_scope: int = 0
    insufficient_evidence: int = 0
    unsafe_poc: int = 0
    unsupported: int = 0


class FinalAuditReport(BaseModel):
    project_id: str
    project_name: str | None = None
    generated_at: datetime
    scope: dict = Field(default_factory=dict)
    total_findings: int
    severity_breakdown: SeverityBreakdown
    validation_breakdown: ValidationBreakdown
    accepted_findings: list[ReportFindingSummary] = Field(default_factory=list)
    rejected_findings: list[ReportFindingSummary] = Field(default_factory=list)
    duplicate_findings: list[ReportFindingSummary] = Field(default_factory=list)
    needs_review_findings: list[ReportFindingSummary] = Field(default_factory=list)
    out_of_scope_findings: list[ReportFindingSummary] = Field(default_factory=list)
    insufficient_evidence_findings: list[ReportFindingSummary] = Field(default_factory=list)
    unsafe_poc_findings: list[ReportFindingSummary] = Field(default_factory=list)
    unsupported_findings: list[ReportFindingSummary] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ReportGenerationResponse(BaseModel):
    project_id: str
    markdown_path: str
    json_path: str
    total_findings: int
    accepted_count: int
    message: str
