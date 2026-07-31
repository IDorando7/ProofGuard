from io import BytesIO
import json
import zipfile
from datetime import datetime, timezone

from app.schemas.finding import Finding
from app.schemas.reproduction import ReproductionResult, ReproductionStatus
from app.schemas.validation import ValidationEvidence, ValidationStatus
from app.services.project_service import project_workspace
from app.services.reproduction_service import save_reproduction_result
from app.services.validation_service import create_validation_decision


SCOPE_YAML = b"""
project_name: MiniLendingProtocol
language: Solidity
framework: Foundry
chain: Ethereum
commit_hash: "local-dev"
contracts_in_scope:
  - src/Vault.sol
contracts_out_of_scope: []
attack_categories:
  - access_control
assets_at_risk:
  - vault funds
"""


def _zip_bytes(files: dict[str, str]) -> BytesIO:
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w") as zip_file:
        for name, content in files.items():
            zip_file.writestr(name, content)
    archive.seek(0)
    return archive


def _create_project(client):
    response = client.post(
        "/projects",
        data={"project_name": "MiniLendingProtocol"},
        files={
            "scope_file": ("scope.yaml", SCOPE_YAML, "application/x-yaml"),
            "repo_zip": (
                "repo.zip",
                _zip_bytes(
                    {
                        "foundry.toml": "[profile.default]\nffi = false\n",
                        "src/Vault.sol": "contract Vault {}",
                    }
                ),
                "application/zip",
            ),
        },
    )
    assert response.status_code == 200
    return response.json()["project_id"]


def _finding(project_id: str):
    return Finding(
        finding_id="finding-1",
        project_id=project_id,
        title="Missing access control on treasury setter",
        category="access_control",
        severity="High",
        confidence=0.8,
        contracts=["src/Vault.sol"],
        functions=["setTreasury"],
        root_cause="missing onlyOwner authorization",
        attack_path="attacker calls setTreasury directly",
        impact="unauthorized privileged action can change treasury",
        recommended_fix="Add onlyOwner.",
        agent_name="access_control_agent",
    )


def _write_artifacts(project_id: str) -> None:
    workspace = project_workspace(project_id)
    finding = _finding(project_id)
    (workspace / "findings" / "findings.json").write_text(
        json.dumps([finding.model_dump(mode="json")], indent=2),
        encoding="utf-8",
    )
    reproduction = ReproductionResult(
        reproduction_id="repr-finding-1",
        finding_id="finding-1",
        project_id=project_id,
        status=ReproductionStatus.REPRODUCED,
        poc_file="test/PoC.t.sol",
        test_name="testName",
        command=["forge", "test", "--match-test", "testName"],
        stdout="stdout",
        stderr=None,
        duration_ms=123,
        error_message=None,
        safety_notes=[],
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    save_reproduction_result(reproduction, workspace)
    create_validation_decision(
        project_id=project_id,
        finding_id="finding-1",
        project_workspace=workspace,
        status=ValidationStatus.ACCEPTED,
        reason="Finding is in scope and has reproduced evidence.",
        confidence=0.95,
        evidence=ValidationEvidence(
            has_finding=True,
            has_reproduction=True,
            reproduction_status="reproduced",
            original_severity="High",
            normalized_severity="High",
        ),
        validator_name="validator_pipeline_v0",
    )


def test_post_final_report_generates_report(client):
    project_id = _create_project(client)
    _write_artifacts(project_id)

    response = client.post(f"/projects/{project_id}/reports/final")

    assert response.status_code == 200
    payload = response.json()
    assert payload["markdown_path"] == "reports/final_report.md"
    assert payload["json_path"] == "reports/final_report.json"
    assert payload["accepted_count"] == 1


def test_get_final_report_returns_json(client):
    project_id = _create_project(client)
    _write_artifacts(project_id)
    client.post(f"/projects/{project_id}/reports/final")

    response = client.get(f"/projects/{project_id}/reports/final")

    assert response.status_code == 200
    assert response.json()["project_id"] == project_id
    assert response.json()["accepted_findings"][0]["finding_id"] == "finding-1"


def test_get_final_report_markdown_returns_markdown(client):
    project_id = _create_project(client)
    _write_artifacts(project_id)
    client.post(f"/projects/{project_id}/reports/final")

    response = client.get(f"/projects/{project_id}/reports/final/markdown")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "# ProofGuard Audit Report" in response.text


def test_missing_project_returns_404(client):
    response = client.post("/projects/missing/reports/final")

    assert response.status_code == 404


def test_missing_report_returns_404(client):
    project_id = _create_project(client)

    response = client.get(f"/projects/{project_id}/reports/final")

    assert response.status_code == 404


def test_report_api_response_does_not_expose_absolute_paths(client):
    project_id = _create_project(client)
    _write_artifacts(project_id)

    response = client.post(f"/projects/{project_id}/reports/final")

    assert response.status_code == 200
    assert "/home/" not in response.text
    assert "data/audits" not in response.text
