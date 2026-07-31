from io import BytesIO
import json
import zipfile

import pytest

from app.schemas.finding import Finding
from app.services.project_service import project_workspace


SCOPE_YAML = b"""
project_name: SubmissionProtocolProject
language: Solidity
framework: Foundry
chain: Ethereum
commit_hash: "local-dev"
contracts_in_scope:
  - src/Treasury.sol
contracts_out_of_scope: []
attack_categories:
  - access_control
  - reentrancy
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


def _create_project(client, name="SubmissionProtocolProject"):
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


def _finding(project_id, finding_id="finding-1", category="access_control"):
    return Finding(
        finding_id=finding_id,
        project_id=project_id,
        title="Missing access control on treasury setter",
        category=category,
        severity="High",
        confidence=0.8,
        contracts=["src/Treasury.sol"],
        functions=["setTreasury"],
        root_cause="Missing onlyOwner authorization",
        attack_path="Attacker calls setTreasury directly",
        impact="Unauthorized treasury modification",
        recommended_fix="Add onlyOwner authorization.",
        agent_name="access_control_agent",
    )


def _write_findings(project_id, findings):
    workspace = project_workspace(project_id)
    findings_dir = workspace / "findings"
    findings_dir.mkdir(parents=True, exist_ok=True)
    (findings_dir / "findings.json").write_text(
        json.dumps([finding.model_dump(mode="json") for finding in findings], indent=2),
        encoding="utf-8",
    )


def _register_node(client, name="agent-one", **updates):
    payload = {
        "node_type": "agent",
        "display_name": name,
        "operator_id": f"operator-{name}",
        "supported_categories": ["access_control"],
    }
    payload.update(updates)
    response = client.post("/nodes", json=payload)
    assert response.status_code == 201
    return response.json()


def _submit(client, route_project_id, node_id, finding_id="finding-1", **updates):
    payload = {
        "finding_id": finding_id,
        "node_id": node_id,
        "agent_name": "access_control_agent",
        "agent_version": "0.1.0",
        "metadata": {"runtime": "local-mvp"},
    }
    payload.update(updates)
    return client.post(f"/projects/{route_project_id}/submissions", json=payload)


def test_create_read_and_list_submission_routes(client):
    project_id = _create_project(client)
    _write_findings(project_id, [_finding(project_id)])
    node = _register_node(client)

    response = _submit(client, project_id, node["node_id"])
    assert response.status_code == 201
    submission = response.json()
    assert submission["submission_id"]
    assert len(submission["finding_hash"]) == 64
    assert submission["status"] == "submitted"
    assert submission["reward_status"] == "pending"

    project_list = client.get(f"/projects/{project_id}/submissions")
    assert project_list.status_code == 200
    assert [item["submission_id"] for item in project_list.json()] == [submission["submission_id"]]

    read = client.get(f"/submissions/{submission['submission_id']}")
    assert read.status_code == 200
    assert read.json() == submission

    node_list = client.get(f"/nodes/{node['node_id']}/submissions")
    assert node_list.status_code == 200
    assert [item["submission_id"] for item in node_list.json()] == [submission["submission_id"]]


def test_project_and_node_list_filters_and_project_isolation(client):
    first_project = _create_project(client, "ProjectOne")
    second_project = _create_project(client, "ProjectTwo")
    _write_findings(first_project, [_finding(first_project)])
    _write_findings(second_project, [_finding(second_project)])
    first_node = _register_node(client)
    second_node = _register_node(client, "agent-two")
    first = _submit(client, first_project, first_node["node_id"]).json()
    second = _submit(client, second_project, second_node["node_id"]).json()

    first_project_items = client.get(f"/projects/{first_project}/submissions").json()
    assert [item["submission_id"] for item in first_project_items] == [first["submission_id"]]
    assert second["submission_id"] not in {item["submission_id"] for item in first_project_items}

    assert len(client.get(f"/projects/{first_project}/submissions?node_id={first_node['node_id']}").json()) == 1
    assert client.get(f"/projects/{first_project}/submissions?node_id={second_node['node_id']}").json() == []
    assert len(client.get(f"/projects/{first_project}/submissions?status=submitted").json()) == 1
    assert len(client.get(f"/projects/{first_project}/submissions?category=Access%20Control").json()) == 1
    assert len(client.get(f"/projects/{first_project}/submissions?reward_status=pending").json()) == 1

    filtered_node = client.get(
        f"/nodes/{first_node['node_id']}/submissions?project_id={first_project}&status=submitted&category=access-control"
    )
    assert [item["submission_id"] for item in filtered_node.json()] == [first["submission_id"]]


def test_missing_submission_node_finding_and_project_return_404(client):
    project_id = _create_project(client)
    _write_findings(project_id, [_finding(project_id)])
    node = _register_node(client)
    assert client.get("/submissions/00000000-0000-0000-0000-000000000000").status_code == 404
    assert client.get("/nodes/00000000-0000-0000-0000-000000000000/submissions").status_code == 404
    assert _submit(client, project_id, node["node_id"], finding_id="missing").status_code == 404
    assert _submit(client, project_id, "00000000-0000-0000-0000-000000000000").status_code == 404
    assert client.get("/projects/missing/submissions").status_code == 404


@pytest.mark.parametrize("status", ["inactive", "suspended", "banned"])
def test_non_active_node_submission_returns_403(client, status):
    project_id = _create_project(client)
    _write_findings(project_id, [_finding(project_id)])
    node = _register_node(client)
    changed = client.post(
        f"/nodes/{node['node_id']}/status",
        json={"status": status, "reason": "Administrative review."},
    )
    assert changed.status_code == 200
    response = _submit(client, project_id, node["node_id"])
    assert response.status_code == 403


def test_validator_only_node_submission_returns_403(client):
    project_id = _create_project(client)
    _write_findings(project_id, [_finding(project_id)])
    validator = _register_node(client, "validator-one", node_type="validator")
    assert _submit(client, project_id, validator["node_id"]).status_code == 403


def test_unsupported_category_returns_400(client):
    project_id = _create_project(client)
    _write_findings(project_id, [_finding(project_id, category="reentrancy")])
    node = _register_node(client)
    assert _submit(client, project_id, node["node_id"]).status_code == 400


def test_duplicate_same_node_submission_returns_409_but_different_node_is_allowed(client):
    project_id = _create_project(client)
    _write_findings(
        project_id,
        [_finding(project_id), _finding(project_id, finding_id="finding-2")],
    )
    first_node = _register_node(client)
    second_node = _register_node(client, "agent-two")
    assert _submit(client, project_id, first_node["node_id"]).status_code == 201
    assert _submit(client, project_id, first_node["node_id"], finding_id="finding-2").status_code == 409
    assert _submit(client, project_id, second_node["node_id"], finding_id="finding-2").status_code == 201


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("project_id", "different-project"),
        ("finding_hash", "a" * 64),
        ("status", "accepted"),
        ("reward_status", "rewarded"),
        ("validation_id", "validation-1"),
        ("private_key", "secret"),
    ],
)
def test_create_route_rejects_client_controlled_fields(client, field, value):
    project_id = _create_project(client)
    _write_findings(project_id, [_finding(project_id)])
    node = _register_node(client)
    response = _submit(client, project_id, node["node_id"], **{field: value})
    assert response.status_code == 422


def test_api_exposes_no_paths_secrets_or_public_status_mutation(client):
    project_id = _create_project(client)
    _write_findings(project_id, [_finding(project_id, finding_id="finding-sensitive")])
    node = _register_node(client)
    response = _submit(client, project_id, node["node_id"], finding_id="finding-sensitive")
    assert response.status_code == 201
    payload = response.json()
    assert "/home/" not in response.text
    assert "data/protocol" not in response.text
    assert "private_key" not in payload
    assert "root_cause" not in payload
    assert "poc_content" not in payload
    status_response = client.patch(
        f"/submissions/{payload['submission_id']}/status",
        json={"status": "accepted", "reason": "Not public."},
    )
    assert status_response.status_code in {404, 405}
