from pathlib import Path
from typing import Any

from app.schemas.deduplication import DeduplicationResult, DeduplicationStatus
from app.schemas.reproduction import ReproductionResult
from app.schemas.scope_validation import ScopeValidationResult, ScopeValidationStatus
from app.schemas.severity import SeverityChangeType, SeverityNormalizationResult
from app.schemas.validation import (
    DuplicateKind,
    ValidationDecision,
    ValidationEvidence,
    ValidationStatus,
)
from app.services.deduplication_service import find_duplicate
from app.services.finding_service import list_project_findings, load_project_finding
from app.services.reproduction_service import load_reproduction_result
from app.services.scope_validation_service import load_scope_from_project_workspace, validate_finding_scope
from app.services.severity_service import normalize_finding_severity
from app.services.validation_service import create_validation_decision, save_validation_decision


VALIDATOR_NAME = "validator_pipeline_v0"


class FindingNotFoundError(FileNotFoundError):
    pass


def validate_finding(
    project_id: str,
    finding_id: str,
    project_workspace: Path,
) -> ValidationDecision:
    findings = list_project_findings(project_workspace)
    finding = next((item for item in findings if item.finding_id == finding_id), None)
    if finding is None:
        raise FindingNotFoundError(f"Finding not found: {finding_id}")

    existing_findings = [item for item in findings if item.finding_id != finding_id]
    return _validate_finding_with_context(
        project_id=project_id,
        finding_id=finding_id,
        project_workspace=project_workspace,
        finding=finding,
        existing_findings=existing_findings,
    )


def validate_all_findings(
    project_id: str,
    project_workspace: Path,
) -> list[ValidationDecision]:
    findings = list_project_findings(project_workspace)
    decisions: list[ValidationDecision] = []
    seen_findings: list[Any] = []

    for finding in findings:
        decision = _validate_finding_with_context(
            project_id=project_id,
            finding_id=finding.finding_id,
            project_workspace=project_workspace,
            finding=finding,
            existing_findings=seen_findings,
        )
        decisions.append(decision)
        seen_findings.append(finding)

    return decisions


def _validate_finding_with_context(
    project_id: str,
    finding_id: str,
    project_workspace: Path,
    finding: Any,
    existing_findings: list[Any],
) -> ValidationDecision:
    reproduction = load_reproduction_result(project_workspace, finding_id)
    scope = load_scope_from_project_workspace(project_workspace)
    scope_result = validate_finding_scope(finding, scope)
    dedup_result = find_duplicate(finding, existing_findings)
    severity_result = normalize_finding_severity(finding, scope, reproduction)
    evidence = _build_evidence(project_workspace, finding, reproduction, scope_result, dedup_result, severity_result)

    status, confidence, reason = _decide_status(scope_result, dedup_result, reproduction, severity_result)
    return _save_decision(
        project_id=project_id,
        finding_id=finding_id,
        project_workspace=project_workspace,
        status=status,
        confidence=confidence,
        reason=reason,
        evidence=evidence,
    )


def _decide_status(
    scope_result: ScopeValidationResult,
    dedup_result: DeduplicationResult,
    reproduction: ReproductionResult | None,
    severity_result: SeverityNormalizationResult,
) -> tuple[ValidationStatus, float, str]:
    if scope_result.status == ScopeValidationStatus.OUT_OF_SCOPE:
        return (
            ValidationStatus.OUT_OF_SCOPE,
            0.95,
            "Finding is outside the declared project scope.",
        )

    if scope_result.status == ScopeValidationStatus.INVALID_SCOPE:
        return (
            ValidationStatus.NEEDS_REVIEW,
            0.30,
            "Project scope is missing or invalid.",
        )

    if reproduction is None:
        return (
            ValidationStatus.NEEDS_REVIEW,
            0.40,
            "Finding has no reproduction result yet.",
        )

    reproduction_status = reproduction.status.value
    if reproduction_status == "failed":
        return (
            ValidationStatus.INSUFFICIENT_EVIDENCE,
            0.65,
            "Reproduction ran but did not reproduce the issue.",
        )
    if reproduction_status == "timeout":
        return (
            ValidationStatus.NEEDS_REVIEW,
            0.50,
            "Reproduction timed out.",
        )
    if reproduction_status == "rejected_unsafe":
        return (
            ValidationStatus.UNSAFE_POC,
            0.90,
            "PoC was rejected by safety checks.",
        )
    if reproduction_status == "unsupported":
        return (
            ValidationStatus.UNSUPPORTED,
            0.80,
            "Project or finding type is unsupported by the current reproduction layer.",
        )
    if reproduction_status == "sandbox_error":
        return (
            ValidationStatus.NEEDS_REVIEW,
            0.45,
            "Sandbox execution failed.",
        )
    if reproduction_status in {"generated", "not_attempted", "running"}:
        return (
            ValidationStatus.NEEDS_REVIEW,
            0.40,
            "Reproduction has not completed yet.",
        )
    if reproduction_status == "reproduced" and scope_result.status == ScopeValidationStatus.IN_SCOPE:
        confidence = _accepted_confidence(dedup_result, severity_result)
        if dedup_result.status == DeduplicationStatus.DUPLICATE:
            return (
                ValidationStatus.ACCEPTED,
                max(0.80, dedup_result.similarity_score),
                "Finding is a valid independent report of an existing root cause.",
            )
        return (
            ValidationStatus.ACCEPTED,
            confidence,
            "Finding is in scope and has reproduced evidence.",
        )
    if reproduction_status == "reproduced":
        return (
            ValidationStatus.NEEDS_REVIEW,
            0.70,
            "Finding reproduced, but scope validation did not clearly pass.",
        )

    return (
        ValidationStatus.NEEDS_REVIEW,
        0.45,
        "Finding requires manual validation review.",
    )


def _accepted_confidence(
    dedup_result: DeduplicationResult,
    severity_result: SeverityNormalizationResult,
) -> float:
    confidence = 0.80 + 0.10
    if severity_result.change_type != SeverityChangeType.DOWNGRADED:
        confidence += 0.05
    if dedup_result.status == DeduplicationStatus.POSSIBLE_DUPLICATE:
        confidence -= 0.10
    return round(max(0.0, min(0.98, confidence)), 4)


def _build_evidence(
    project_workspace: Path,
    finding: Any,
    reproduction: ReproductionResult | None,
    scope_result: ScopeValidationResult,
    dedup_result: DeduplicationResult,
    severity_result: SeverityNormalizationResult,
) -> ValidationEvidence:
    finding_id = getattr(finding, "finding_id", None) or "unknown"
    notes = [
        *scope_result.notes,
        *dedup_result.notes,
        *severity_result.notes,
    ]

    if dedup_result.status == DeduplicationStatus.POSSIBLE_DUPLICATE:
        notes.append("Finding is a possible duplicate and may require review.")
    if reproduction is None:
        notes.append("No reproduction result was found for this finding.")
    elif reproduction.safety_notes:
        notes.extend(reproduction.safety_notes)

    independent_duplicate = (
        dedup_result.status == DeduplicationStatus.DUPLICATE
        and reproduction is not None
        and reproduction.status.value == "reproduced"
        and scope_result.status == ScopeValidationStatus.IN_SCOPE
    )
    return ValidationEvidence(
        has_finding=True,
        has_reproduction=reproduction is not None,
        reproduction_status=reproduction.status.value if reproduction is not None else None,
        has_poc_file=bool(reproduction and reproduction.poc_file),
        has_stdout=_has_reproduction_output(project_workspace, finding_id, reproduction, "stdout"),
        has_stderr=_has_reproduction_output(project_workspace, finding_id, reproduction, "stderr"),
        in_scope=_scope_evidence_value(scope_result),
        is_duplicate=dedup_result.is_duplicate,
        duplicate_of=dedup_result.duplicate_of,
        duplicate_kind=(
            DuplicateKind.INDEPENDENT_ROOT_CAUSE
            if independent_duplicate
            else None
        ),
        is_valid_duplicate=True if independent_duplicate else None,
        canonical_finding_id=(
            dedup_result.duplicate_of if independent_duplicate else None
        ),
        original_severity=severity_result.original_severity.value,
        normalized_severity=severity_result.normalized_severity.value,
        notes=notes,
    )


def _scope_evidence_value(scope_result: ScopeValidationResult) -> bool | None:
    if scope_result.status == ScopeValidationStatus.IN_SCOPE:
        return True
    if scope_result.status == ScopeValidationStatus.OUT_OF_SCOPE:
        return False
    return None


def _has_reproduction_output(
    project_workspace: Path,
    finding_id: str,
    reproduction: ReproductionResult | None,
    field: str,
) -> bool:
    if reproduction is None:
        return False
    value = getattr(reproduction, field, None)
    if value:
        return True
    return (project_workspace / "reproductions" / finding_id / f"{field}.txt").is_file()


def _save_decision(
    project_id: str,
    finding_id: str,
    project_workspace: Path,
    status: ValidationStatus,
    confidence: float,
    reason: str,
    evidence: ValidationEvidence,
) -> ValidationDecision:
    existing = create_validation_decision(
        project_id=project_id,
        finding_id=finding_id,
        project_workspace=project_workspace,
        status=status,
        reason=reason,
        confidence=confidence,
        evidence=evidence,
        validator_name=VALIDATOR_NAME,
    )
    return save_validation_decision(existing, project_workspace)
