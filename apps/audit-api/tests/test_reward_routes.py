from app.core.paths import protocol_data_root
from app.main import app
from app.schemas.reproduction import ReproductionStatus
from app.schemas.submission import SubmissionStatusUpdate
from app.schemas.validation import ValidationEvidence, ValidationStatus
from app.services.node_registry_service import load_node, save_node
from app.services.project_service import project_workspace
from app.services.reproduction_service import create_initial_reproduction_result, save_reproduction_result
from app.services.submission_service import update_submission_status
from app.services.validation_service import create_validation_decision
from test_contribution_routes import (
    _create_project,
    _prepare_eligible_submission,
    _register_node,
    _write_finding,
)


def _prepare_unsafe_submission(client):
    project_id = _create_project(client, "UnsafeRewardProject")
    finding = _write_finding(project_id)
    node = _register_node(client, "unsafe-reward-agent")
    response = client.post(
        f"/projects/{project_id}/submissions",
        json={"finding_id": finding.finding_id, "node_id": node["node_id"]},
    )
    assert response.status_code == 201
    submission = response.json()
    root = app.dependency_overrides[protocol_data_root]()
    workspace = project_workspace(project_id)
    reproduction = create_initial_reproduction_result(project_id, finding.finding_id, workspace)
    reproduction = save_reproduction_result(
        reproduction.model_copy(update={"status": ReproductionStatus.REJECTED_UNSAFE}),
        workspace,
    )
    validation = create_validation_decision(
        project_id,
        finding.finding_id,
        workspace,
        status=ValidationStatus.UNSAFE_POC,
        reason="Safety preflight rejected the payload.",
        evidence=ValidationEvidence(
            has_finding=True,
            has_reproduction=True,
            reproduction_status="rejected_unsafe",
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
            status="unsafe",
            reason="Unsafe.",
            validation_id=validation.validation_id,
        ),
    )
    return project_id, node, submission


def test_reward_cycle_routes_finalize_and_read_idempotently(client):
    project_id, node, submission, _ = _prepare_eligible_submission(client)
    score = client.post(f"/submissions/{submission['submission_id']}/contribution/calculate")
    assert score.status_code == 200

    created = client.post(
        f"/projects/{project_id}/reward-cycles",
        json={"reward_pool": 1000, "description": "Protocol reward simulation."},
    )
    assert created.status_code == 201
    cycle = created.json()
    assert cycle["status"] == "draft"
    calculated = client.post(f"/reward-cycles/{cycle['cycle_id']}/calculate")
    assert calculated.status_code == 200
    assert calculated.json()["total_allocated"] == 1000
    finalized = client.post(f"/reward-cycles/{cycle['cycle_id']}/finalize")
    assert finalized.status_code == 200
    assert finalized.json()["status"] == "finalized"
    repeated = client.post(f"/reward-cycles/{cycle['cycle_id']}/finalize")
    assert repeated.status_code == 200
    assert repeated.json() == finalized.json()

    assert client.get(f"/reward-cycles/{cycle['cycle_id']}").json() == finalized.json()
    listed = client.get(f"/projects/{project_id}/reward-cycles?status=finalized")
    assert [item["cycle_id"] for item in listed.json()] == [cycle["cycle_id"]]
    event = client.get(f"/submissions/{submission['submission_id']}/reward")
    assert event.status_code == 200
    assert event.json()["reward_unit"] == "protocol_points"
    summary = client.get(f"/nodes/{node['node_id']}/rewards")
    assert summary.status_code == 200
    assert summary.json()["total_protocol_points"] == 1000
    assert "/home/" not in finalized.text
    assert all(key not in finalized.text for key in ("wallet_address", "token_address", "transaction_hash"))


def test_reward_routes_reject_client_economic_controls_and_source_changes(client):
    project_id, node, submission, _ = _prepare_eligible_submission(client)
    client.post(f"/submissions/{submission['submission_id']}/contribution/calculate")
    assert client.post(
        f"/projects/{project_id}/reward-cycles",
        json={"project_id": "other", "reward_pool": 1000},
    ).status_code == 422
    assert client.post(
        f"/projects/{project_id}/reward-cycles",
        json={"reward_pool": 1000, "reward_amount": 999},
    ).status_code == 422
    cycle = client.post(f"/projects/{project_id}/reward-cycles", json={"reward_pool": 1000}).json()
    assert client.post(
        f"/reward-cycles/{cycle['cycle_id']}/calculate",
        json={"reputation_multiplier": 2},
    ).status_code == 422
    assert client.post(f"/reward-cycles/{cycle['cycle_id']}/calculate").status_code == 200
    root = app.dependency_overrides[protocol_data_root]()
    stored_node = load_node(root, node["node_id"])
    save_node(root, stored_node.model_copy(update={"reputation_score": .9}))
    assert client.post(f"/reward-cycles/{cycle['cycle_id']}/finalize").status_code == 409
    assert client.get("/reward-cycles/missing").status_code == 404
    assert client.get("/nodes/missing/rewards").status_code == 404
    assert client.patch("/reward-events/nope", json={"reward_amount": 1}).status_code in {404, 405}


def test_penalty_routes_create_read_list_and_reject_custom_values(client):
    _, node, submission = _prepare_unsafe_submission(client)
    custom = client.post(
        f"/submissions/{submission['submission_id']}/penalty/process",
        json={"protocol_penalty_points": 999},
    )
    assert custom.status_code == 422
    processed = client.post(f"/submissions/{submission['submission_id']}/penalty/process")
    assert processed.status_code == 200
    assert processed.json()["created"] is True
    assert processed.json()["event"]["protocol_penalty_points"] == 25
    assert processed.json()["event"]["simulated_stake_loss"] == 0
    assert processed.json()["event"]["executed_onchain"] is False
    repeated = client.post(f"/submissions/{submission['submission_id']}/penalty/process")
    assert repeated.status_code == 200
    assert repeated.json()["created"] is False
    assert client.get(f"/submissions/{submission['submission_id']}/penalty").status_code == 200
    penalties = client.get(f"/nodes/{node['node_id']}/penalties")
    assert penalties.status_code == 200
    assert len(penalties.json()) == 1
    assert client.patch("/penalty-events/nope", json={"protocol_penalty_points": 1}).status_code in {404, 405}


def test_non_unsafe_penalty_is_not_applicable(client):
    _, _, submission, _ = _prepare_eligible_submission(client)
    assert client.post(f"/submissions/{submission['submission_id']}/penalty/process").status_code == 409
    assert client.get(f"/submissions/{submission['submission_id']}/penalty").status_code == 404
