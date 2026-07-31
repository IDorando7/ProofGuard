from io import BytesIO
import json
import zipfile

from app.core.paths import protocol_data_root
from app.main import app
from app.schemas.finding import Finding
from app.schemas.reproduction import ReproductionStatus
from app.schemas.submission import SubmissionStatusUpdate
from app.schemas.validation import ValidationEvidence, ValidationStatus
from app.services.project_service import project_workspace
from app.services.reproduction_service import create_initial_reproduction_result, save_reproduction_result
from app.services.submission_service import update_submission_status
from app.services.validation_service import create_validation_decision


SCOPE_YAML = b"""
project_name: ContributionProject
language: Solidity
framework: Foundry
chain: Ethereum
commit_hash: "local-dev"
contracts_in_scope:
  - src/Treasury.sol
contracts_out_of_scope: []
attack_categories:
  - access_control
assets_at_risk:
  - treasury funds
"""


def _zip_bytes():
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w") as zip_file:
        zip_file.writestr("foundry.toml", "[profile.default]\nffi = false\n")
        zip_file.writestr("src/Treasury.sol", "contract Treasury {}")
    archive.seek(0)
    return archive


def _create_project(client, name="ContributionProject"):
    response = client.post(
        "/projects",
        data={"project_name": name},
        files={
            "scope_file": ("scope.yaml", SCOPE_YAML, "application/x-yaml"),
            "repo_zip": ("repo.zip", _zip_bytes(), "application/zip"),
        },
    )
    assert response.status_code == 200
    return response.json()["project_id"]


def _write_finding(project_id, finding_id="finding-1"):
    finding = Finding(
        finding_id=finding_id,
        project_id=project_id,
        title="Missing access control on treasury setter",
        category="access_control",
        severity="High",
        confidence=0.8,
        contracts=["src/Treasury.sol"],
        functions=["setTreasury"],
        root_cause="Missing onlyOwner authorization check",
        attack_path="Attacker calls setTreasury directly",
        impact="Unauthorized treasury modification",
        recommended_fix="Add onlyOwner authorization.",
        agent_name="access_control_agent",
    )
    findings_dir = project_workspace(project_id) / "findings"
    findings_dir.mkdir(parents=True, exist_ok=True)
    (findings_dir / "findings.json").write_text(
        json.dumps([finding.model_dump(mode="json")], indent=2),
        encoding="utf-8",
    )
    return finding


def _register_node(client, name="agent-one"):
    response = client.post(
        "/nodes",
        json={
            "node_type": "agent",
            "display_name": name,
            "operator_id": f"operator-{name}",
            "supported_categories": ["access_control"],
        },
    )
    assert response.status_code == 201
    return response.json()


def _prepare_eligible_submission(client, project_name="ContributionProject", node_name="agent-one"):
    project_id = _create_project(client, project_name)
    finding = _write_finding(project_id)
    node = _register_node(client, node_name)
    submitted = client.post(
        f"/projects/{project_id}/submissions",
        json={"finding_id": finding.finding_id, "node_id": node["node_id"]},
    )
    assert submitted.status_code == 201
    submission = submitted.json()
    root = app.dependency_overrides[protocol_data_root]()
    workspace = project_workspace(project_id)
    reproduction = create_initial_reproduction_result(project_id, finding.finding_id, workspace)
    reproduction = save_reproduction_result(
        reproduction.model_copy(update={"status": ReproductionStatus.REPRODUCED}),
        workspace,
    )
    validation = create_validation_decision(
        project_id,
        finding.finding_id,
        workspace,
        status=ValidationStatus.ACCEPTED,
        reason="Accepted by deterministic validation.",
        evidence=ValidationEvidence(
            has_finding=True,
            has_reproduction=True,
            reproduction_status="reproduced",
            in_scope=True,
            is_duplicate=False,
            normalized_severity="High",
        ),
    )
    update_submission_status(
        root,
        submission["submission_id"],
        SubmissionStatusUpdate(
            status="validation_pending",
            reason="Pending validation.",
            reproduction_id=reproduction.reproduction_id,
        ),
    )
    update_submission_status(
        root,
        submission["submission_id"],
        SubmissionStatusUpdate(
            status="accepted",
            reason="Accepted.",
            validation_id=validation.validation_id,
        ),
    )
    return project_id, node, submission, validation


def test_calculate_read_and_list_contribution_routes(client):
    project_id, node, submission, _ = _prepare_eligible_submission(client)
    response = client.post(f"/submissions/{submission['submission_id']}/contribution/calculate")
    assert response.status_code == 200
    score = response.json()
    assert score["total_score"] == 100
    assert score["eligibility_status"] == "eligible"
    assert score["eligible_for_reward"] is True
    assert score["components"]
    assert score["signals"]
    assert "reward_amount" not in score
    assert "/home/" not in response.text

    read = client.get(f"/submissions/{submission['submission_id']}/contribution")
    assert read.status_code == 200
    assert read.json() == score

    project_scores = client.get(
        f"/projects/{project_id}/contributions?node_id={node['node_id']}&eligibility_status=eligible&eligible_for_reward=true"
    )
    assert project_scores.status_code == 200
    assert [item["score_id"] for item in project_scores.json()] == [score["score_id"]]

    node_scores = client.get(
        f"/nodes/{node['node_id']}/contributions?project_id={project_id}&eligibility_status=eligible"
    )
    assert node_scores.status_code == 200
    assert [item["score_id"] for item in node_scores.json()] == [score["score_id"]]


def test_missing_resources_and_project_return_404(client):
    missing = "00000000-0000-0000-0000-000000000000"
    assert client.post(f"/submissions/{missing}/contribution/calculate").status_code == 404
    assert client.get(f"/submissions/{missing}/contribution").status_code == 404
    assert client.get(f"/nodes/{missing}/contributions").status_code == 404
    assert client.get("/projects/missing/contributions").status_code == 404


def test_missing_finding_returns_404(client):
    project_id, _, submission, _ = _prepare_eligible_submission(client)
    (project_workspace(project_id) / "findings" / "findings.json").unlink()
    response = client.post(f"/submissions/{submission['submission_id']}/contribution/calculate")
    assert response.status_code == 404


def test_input_mismatch_returns_409(client):
    project_id, _, submission, validation = _prepare_eligible_submission(client)
    path = project_workspace(project_id) / "validations" / "finding-1" / "validation.json"
    payload = validation.model_dump(mode="json")
    payload["project_id"] = "other-project"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    response = client.post(f"/submissions/{submission['submission_id']}/contribution/calculate")
    assert response.status_code == 409


def test_calculation_rejects_client_score_values_and_has_no_edit_route(client):
    _, _, submission, _ = _prepare_eligible_submission(client)
    calculate = client.post(
        f"/submissions/{submission['submission_id']}/contribution/calculate",
        json={"total_score": 100, "eligible_for_reward": True},
    )
    assert calculate.status_code == 422
    edit = client.patch(
        f"/submissions/{submission['submission_id']}/contribution",
        json={"total_score": 100},
    )
    assert edit.status_code in {404, 405}


def test_project_listing_excludes_other_projects(client):
    first_project, _, first_submission, _ = _prepare_eligible_submission(client, "FirstProject", "agent-first")
    second_project, _, second_submission, _ = _prepare_eligible_submission(client, "SecondProject", "agent-second")
    client.post(f"/submissions/{first_submission['submission_id']}/contribution/calculate")
    client.post(f"/submissions/{second_submission['submission_id']}/contribution/calculate")
    first_scores = client.get(f"/projects/{first_project}/contributions").json()
    assert [item["submission_id"] for item in first_scores] == [first_submission["submission_id"]]
    assert second_project != first_project
