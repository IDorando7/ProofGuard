import pytest
from pydantic import ValidationError

from app.schemas.finding import (
    FindingCategory,
    FindingCreate,
    FindingSeverity,
    FindingStatus,
)


def _valid_finding_payload() -> dict:
    return {
        "title": "Unchecked owner access",
        "category": FindingCategory.ACCESS_CONTROL,
        "severity": FindingSeverity.HIGH,
        "confidence": 0.8,
        "contracts": ["src/Vault.sol"],
        "functions": ["withdraw"],
        "root_cause": "The withdraw function lacks an owner check.",
        "attack_path": "An attacker calls withdraw directly.",
        "impact": "Funds can be drained from the vault.",
        "conditions": "Vault contains user deposits.",
        "reproduction_steps": ["Deploy vault", "Call withdraw as attacker"],
        "poc_type": "none",
        "poc_file": None,
        "recommended_fix": "Add an explicit access control check.",
        "status": FindingStatus.CANDIDATE,
        "agent_name": "test_agent",
    }


def test_valid_finding_create_passes_validation():
    finding = FindingCreate(**_valid_finding_payload())

    assert finding.title == "Unchecked owner access"
    assert finding.category == FindingCategory.ACCESS_CONTROL
    assert finding.status == FindingStatus.CANDIDATE


def test_confidence_below_zero_fails():
    payload = _valid_finding_payload()
    payload["confidence"] = -0.1

    with pytest.raises(ValidationError):
        FindingCreate(**payload)


def test_confidence_above_one_fails():
    payload = _valid_finding_payload()
    payload["confidence"] = 1.1

    with pytest.raises(ValidationError):
        FindingCreate(**payload)


def test_invalid_category_fails():
    payload = _valid_finding_payload()
    payload["category"] = "invalid_category"

    with pytest.raises(ValidationError):
        FindingCreate(**payload)


def test_invalid_status_valid_fails():
    payload = _valid_finding_payload()
    payload["status"] = "valid"

    with pytest.raises(ValidationError):
        FindingCreate(**payload)

