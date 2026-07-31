import json
from pathlib import Path
from uuid import UUID

from app.schemas.finding import FindingCategory, FindingCreate, FindingStatus
from app.services.finding_service import save_findings_to_file


def _finding() -> FindingCreate:
    return FindingCreate(
        title="Mock candidate finding",
        category=FindingCategory.OTHER,
        confidence=0.5,
        contracts=["src/Vault.sol"],
        root_cause="This is a mock root cause.",
        attack_path="This is a mock attack path.",
        impact="This is a mock impact.",
        recommended_fix="Replace this mock recommendation.",
        agent_name="mock_agent",
    )


def test_save_findings_to_file_creates_json_file(tmp_path: Path):
    output_dir = tmp_path / "findings"

    save_findings_to_file("project-1", [_finding()], output_dir, "findings.json")

    assert (output_dir / "findings.json").is_file()


def test_save_findings_to_file_adds_finding_id_and_project_id(tmp_path: Path):
    enriched = save_findings_to_file(
        "project-1",
        [_finding()],
        tmp_path / "findings",
        "findings.json",
    )

    assert len(enriched) == 1
    assert enriched[0].project_id == "project-1"
    assert UUID(enriched[0].finding_id)


def test_saved_json_can_be_loaded_back_with_expected_fields(tmp_path: Path):
    output_dir = tmp_path / "findings"

    enriched = save_findings_to_file(
        "project-1",
        [_finding()],
        output_dir,
        "findings.json",
    )

    saved = json.loads((output_dir / "findings.json").read_text(encoding="utf-8"))

    assert saved == [
        {
            "title": "Mock candidate finding",
            "category": "other",
            "severity": "Informational",
            "confidence": 0.5,
            "contracts": ["src/Vault.sol"],
            "functions": [],
            "root_cause": "This is a mock root cause.",
            "attack_path": "This is a mock attack path.",
            "impact": "This is a mock impact.",
            "conditions": None,
            "reproduction_steps": [],
            "poc_type": "none",
            "poc_file": None,
            "recommended_fix": "Replace this mock recommendation.",
            "status": "candidate",
            "agent_name": "mock_agent",
            "finding_id": enriched[0].finding_id,
            "project_id": "project-1",
        }
    ]
    assert saved[0]["finding_id"]
    assert saved[0]["project_id"] == "project-1"
    assert saved[0]["status"] == FindingStatus.CANDIDATE.value
