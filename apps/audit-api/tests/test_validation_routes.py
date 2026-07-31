from io import BytesIO
import json
from pathlib import Path
import zipfile

from app.schemas.finding import Finding
from app.schemas.reproduction import ReproductionResult, ReproductionStatus
from app.services.project_service import project_workspace
from app.services.reproduction_service import save_reproduction_result


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


def _finding(finding_id="finding-1", function="setTreasury"):
    return Finding(
        finding_id=finding_id,
        project_id="project-1",
        title="Missing access control on treasury setter",
        category="access_control",
        severity="High",
        confidence=0.8,
        contracts=["src/Vault.sol"],
        functions=[function],
        root_cause="missing onlyOwner authorization",
        attack_path="attacker calls setTreasury directly",
        impact="unauthorized privileged action can change treasury",
        recommended_fix="Add onlyOwner.",
        agent_name="access_control_agent",
    )


def _write_findings(project_id: str, findings: list[Finding]) -> None:
    workspace = project_workspace(project_id)
    (workspace / "findings").mkdir(parents=True, exist_ok=True)
    normalized = [finding.model_copy(update={"project_id": project_id}) for finding in findings]
    (workspace / "findings" / "findings.json").write_text(
        json.dumps([finding.model_dump(mode="json") for finding in normalized], indent=2),
        encoding="utf-8",
    )


def _save_reproduction(project_id: str, finding_id="finding-1", status=ReproductionStatus.REPRODUCED) -> None:
    workspace = project_workspace(project_id)
    result = ReproductionResult(
        reproduction_id=f"repr-{finding_id}",
        finding_id=finding_id,
        project_id=project_id,
        status=status,
        poc_file="test/PoC.t.sol",
        test_name="testName",
        command=["forge", "test", "--match-test", "testName"],
        stdout="stdout",
        stderr=None,
        duration_ms=123,
        error_message=None,
        safety_notes=[],
        created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        updated_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )
    save_reproduction_result(result, workspace)


def test_post_validate_returns_decision(client):
    project_id = _create_project(client)
    _write_findings(project_id, [_finding()])
    _save_reproduction(project_id)

    response = client.post(f"/projects/{project_id}/findings/finding-1/validate")

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"


def test_get_validation_returns_saved_decision(client):
    project_id = _create_project(client)
    _write_findings(project_id, [_finding()])
    _save_reproduction(project_id)
    client.post(f"/projects/{project_id}/findings/finding-1/validate")

    response = client.get(f"/projects/{project_id}/findings/finding-1/validation")

    assert response.status_code == 200
    assert response.json()["finding_id"] == "finding-1"


def test_get_validations_returns_list(client):
    project_id = _create_project(client)
    _write_findings(project_id, [_finding()])
    _save_reproduction(project_id)
    client.post(f"/projects/{project_id}/findings/finding-1/validate")

    response = client.get(f"/projects/{project_id}/validations")

    assert response.status_code == 200
    assert len(response.json()) == 1


def test_validate_all_returns_list(client):
    project_id = _create_project(client)
    _write_findings(project_id, [_finding("finding-1"), _finding("finding-2", function="mint")])
    _save_reproduction(project_id, "finding-1")
    _save_reproduction(project_id, "finding-2")

    response = client.post(f"/projects/{project_id}/validate-all")

    assert response.status_code == 200
    assert len(response.json()) == 2


def test_missing_project_returns_404(client):
    response = client.post("/projects/missing/findings/finding-1/validate")

    assert response.status_code == 404


def test_missing_finding_returns_404(client):
    project_id = _create_project(client)

    response = client.post(f"/projects/{project_id}/findings/missing/validate")

    assert response.status_code == 404


def test_validation_api_response_does_not_expose_absolute_paths(client):
    project_id = _create_project(client)
    _write_findings(project_id, [_finding()])
    _save_reproduction(project_id)

    response = client.post(f"/projects/{project_id}/findings/finding-1/validate")

    assert response.status_code == 200
    assert "/home/" not in response.text
    assert "data/audits" not in response.text
