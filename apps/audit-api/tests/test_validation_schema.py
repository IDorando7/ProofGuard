import pytest
from pydantic import ValidationError

from app.schemas.validation import (
    ValidationDecisionCreate,
    ValidationEvidence,
    ValidationStatus,
)


def test_validation_decision_create_accepts_minimal_valid_data():
    decision = ValidationDecisionCreate(
        project_id="project-1",
        finding_id="finding-1",
        reason="Initial validation pending.",
    )

    assert decision.project_id == "project-1"
    assert decision.finding_id == "finding-1"
    assert decision.reason == "Initial validation pending."


def test_default_status_is_needs_review():
    decision = ValidationDecisionCreate(
        project_id="project-1",
        finding_id="finding-1",
        reason="Initial validation pending.",
    )

    assert decision.status == ValidationStatus.NEEDS_REVIEW


def test_confidence_zero_is_valid():
    decision = ValidationDecisionCreate(
        project_id="project-1",
        finding_id="finding-1",
        reason="Initial validation pending.",
        confidence=0.0,
    )

    assert decision.confidence == 0.0


def test_confidence_one_is_valid():
    decision = ValidationDecisionCreate(
        project_id="project-1",
        finding_id="finding-1",
        reason="Initial validation pending.",
        confidence=1.0,
    )

    assert decision.confidence == 1.0


def test_confidence_below_zero_fails_validation():
    with pytest.raises(ValidationError):
        ValidationDecisionCreate(
            project_id="project-1",
            finding_id="finding-1",
            reason="Initial validation pending.",
            confidence=-0.1,
        )


def test_confidence_above_one_fails_validation():
    with pytest.raises(ValidationError):
        ValidationDecisionCreate(
            project_id="project-1",
            finding_id="finding-1",
            reason="Initial validation pending.",
            confidence=1.1,
        )


def test_empty_reason_fails_validation():
    with pytest.raises(ValidationError):
        ValidationDecisionCreate(
            project_id="project-1",
            finding_id="finding-1",
            reason="",
        )


def test_invalid_status_fails_validation():
    with pytest.raises(ValidationError):
        ValidationDecisionCreate(
            project_id="project-1",
            finding_id="finding-1",
            reason="Initial validation pending.",
            status="confirmed",
        )


def test_duplicate_status_can_include_duplicate_of_in_evidence():
    decision = ValidationDecisionCreate(
        project_id="project-1",
        finding_id="finding-2",
        status=ValidationStatus.DUPLICATE,
        reason="Duplicate of previous access control finding.",
        evidence=ValidationEvidence(is_duplicate=True, duplicate_of="finding-1"),
    )

    assert decision.status == ValidationStatus.DUPLICATE
    assert decision.evidence.duplicate_of == "finding-1"


def test_accepted_status_can_include_reproduction_evidence():
    decision = ValidationDecisionCreate(
        project_id="project-1",
        finding_id="finding-1",
        status=ValidationStatus.ACCEPTED,
        confidence=0.9,
        reason="Finding has reproduced evidence.",
        evidence=ValidationEvidence(
            has_finding=True,
            has_reproduction=True,
            reproduction_status="reproduced",
            has_poc_file=True,
        ),
    )

    assert decision.status == ValidationStatus.ACCEPTED
    assert decision.evidence.reproduction_status == "reproduced"

