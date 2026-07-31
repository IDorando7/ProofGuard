from io import BytesIO
import json
import zipfile

from app.core.paths import protocol_data_root
from app.main import app
from app.schemas.finding import Finding
from app.schemas.reproduction import ReproductionStatus
from app.schemas.submission import SubmissionStatusUpdate
from app.schemas.validation import ValidationEvidence, ValidationStatus
from app.services.contribution_scoring_service import (
    calculate_contribution_for_submission,
    load_contribution_score,
    save_contribution_score,
)
from app.services.project_service import project_workspace
from app.services.reproduction_service import create_initial_reproduction_result, save_reproduction_result
from app.services.submission_service import update_submission_status
from app.services.validation_service import create_validation_decision


SCOPE_YAML = b"""
project_name: ReputationProject
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


def _create_project(client, name="ReputationProject"):
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


def _prepare(
    client,
    validation_status="accepted",
    reproduction_status="reproduced",
    create_contribution=True,
    project_name="ReputationProject",
    node_name="agent-one",
):
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
    reproduction = None
    if reproduction_status is not None:
        reproduction = create_initial_reproduction_result(project_id, finding.finding_id, workspace)
        reproduction = save_reproduction_result(
            reproduction.model_copy(update={"status": ReproductionStatus(reproduction_status)}),
            workspace,
        )
    validation = create_validation_decision(
        project_id,
        finding.finding_id,
        workspace,
        status=ValidationStatus(validation_status),
        reason="Deterministic validation.",
        evidence=ValidationEvidence(
            has_finding=True,
            has_reproduction=reproduction is not None,
            reproduction_status=reproduction_status,
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
            reproduction_id=reproduction.reproduction_id if reproduction else None,
        ),
    )
    update_submission_status(
        root,
        submission["submission_id"],
        SubmissionStatusUpdate(
            status=validation_status,
            reason="Validation finalized.",
            validation_id=validation.validation_id,
        ),
    )
    if create_contribution:
        calculate_contribution_for_submission(root, workspace, submission["submission_id"])
    return project_id, node, submission, validation


def test_process_read_history_and_event_routes(client):
    project_id, node, submission, _ = _prepare(client)
    response = client.post(f"/submissions/{submission['submission_id']}/reputation/process")
    assert response.status_code == 200
    result = response.json()
    assert result["processing_status"] == "applied"
    assert result["previous_reputation"] == 0.5
    assert result["reputation_delta"] == 0.06
    assert result["current_reputation"] == 0.56
    assert result["event_id"]
    assert result["event"]["category"] == "access_control"
    assert "reward_amount" not in result["event"]
    assert "/home/" not in response.text

    current = client.get(f"/nodes/{node['node_id']}/reputation")
    assert current.status_code == 200
    assert current.json()["current_reputation"] == 0.56
    assert current.json()["statistics"]["total_submissions"] == 1
    assert current.json()["total_reputation_events"] == 1

    history = client.get(
        f"/nodes/{node['node_id']}/reputation/history?category=access_control&event_type=accepted_contribution&application_status=applied"
    )
    assert history.status_code == 200
    assert [item["event_id"] for item in history.json()] == [result["event_id"]]

    event = client.get(f"/reputation/events/{result['event_id']}")
    assert event.status_code == 200
    assert event.json() == result["event"]

    project_events = client.get(f"/projects/{project_id}/reputation/events?node_id={node['node_id']}")
    assert [item["event_id"] for item in project_events.json()] == [result["event_id"]]


def test_repeated_processing_is_idempotent_through_api(client):
    _, node, submission, _ = _prepare(client)
    first = client.post(f"/submissions/{submission['submission_id']}/reputation/process").json()
    second_response = client.post(f"/submissions/{submission['submission_id']}/reputation/process")
    assert second_response.status_code == 200
    second = second_response.json()
    assert second["processing_status"] == "already_applied"
    assert second["event_id"] == first["event_id"]
    current = client.get(f"/nodes/{node['node_id']}/reputation").json()
    assert current["current_reputation"] == 0.56
    assert current["statistics"]["total_submissions"] == 1


def test_pending_validation_returns_no_event_or_delta(client):
    _, node, submission, _ = _prepare(
        client,
        validation_status="needs_review",
        reproduction_status=None,
    )
    response = client.post(f"/submissions/{submission['submission_id']}/reputation/process")
    assert response.status_code == 200
    result = response.json()
    assert result["processing_status"] == "pending"
    assert result["reputation_delta"] == 0
    assert result["event_id"] is None
    assert result["event"] is None
    current = client.get(f"/nodes/{node['node_id']}/reputation").json()
    assert current["current_reputation"] == 0.5
    assert current["statistics"]["total_submissions"] == 0


def test_missing_submission_node_contribution_and_event_errors(client):
    missing = "00000000-0000-0000-0000-000000000000"
    assert client.post(f"/submissions/{missing}/reputation/process").status_code == 404
    assert client.get(f"/nodes/{missing}/reputation").status_code == 404
    assert client.get(f"/nodes/{missing}/reputation/history").status_code == 404
    assert client.get(f"/reputation/events/{missing}").status_code == 404

    _, _, submission, _ = _prepare(client, create_contribution=False)
    assert client.post(f"/submissions/{submission['submission_id']}/reputation/process").status_code == 409


def test_missing_node_during_processing_returns_404(client):
    _, node, submission, _ = _prepare(client)
    root = app.dependency_overrides[protocol_data_root]()
    (root / "nodes" / node["node_id"] / "node.json").unlink()
    assert client.post(f"/submissions/{submission['submission_id']}/reputation/process").status_code == 404


def test_input_mismatch_and_changed_source_return_conflict(client):
    _, _, submission, _ = _prepare(client, project_name="MismatchProject", node_name="agent-mismatch")
    root = app.dependency_overrides[protocol_data_root]()
    contribution = load_contribution_score(root, submission["submission_id"])
    save_contribution_score(root, contribution.model_copy(update={"node_id": "other-node"}))
    assert client.post(f"/submissions/{submission['submission_id']}/reputation/process").status_code == 409

    _, _, second_submission, _ = _prepare(client, project_name="ChangedProject", node_name="agent-changed")
    first = client.post(f"/submissions/{second_submission['submission_id']}/reputation/process")
    assert first.status_code == 200
    contribution = load_contribution_score(root, second_submission["submission_id"])
    save_contribution_score(root, contribution.model_copy(update={"total_score": contribution.total_score - 1}))
    assert client.post(f"/submissions/{second_submission['submission_id']}/reputation/process").status_code == 409


def test_history_excludes_other_nodes_and_supports_empty_filter(client):
    _, first_node, first_submission, _ = _prepare(client, project_name="HistoryOne", node_name="history-one")
    client.post(f"/submissions/{first_submission['submission_id']}/reputation/process")
    second_node = _register_node(client, "history-two")
    assert len(client.get(f"/nodes/{first_node['node_id']}/reputation/history").json()) == 1
    assert client.get(f"/nodes/{second_node['node_id']}/reputation/history").json() == []
    assert client.get(
        f"/nodes/{first_node['node_id']}/reputation/history?category=reentrancy"
    ).json() == []


def test_process_body_and_public_edit_routes_reject_client_control(client):
    _, node, submission, _ = _prepare(client)
    response = client.post(
        f"/submissions/{submission['submission_id']}/reputation/process",
        json={"reputation_delta": 1, "statistics": {"total_submissions": 99}},
    )
    assert response.status_code == 422
    assert client.patch(
        f"/nodes/{node['node_id']}/reputation",
        json={"current_reputation": 1},
    ).status_code in {404, 405}
    assert client.post(
        f"/nodes/{node['node_id']}/reputation/set",
        json={"current_reputation": 1},
    ).status_code in {404, 405}
