from app.core.paths import protocol_data_root
from app.main import app
from app.services.local_simulator_service import (
    bootstrap_local_simulator_routing_capacity,
)
from tests.test_project_creation import _create_project, _zip_bytes


VULNERABLE_SOURCE = """
contract Vault {
    address treasury;
    mapping(address => uint256) balances;
    function setTreasury(address value) external { treasury = value; }
    function withdraw() external {
        payable(msg.sender).call{value: balances[msg.sender]}(\"\");
        balances[msg.sender] = 0;
    }
}
"""


def _protocol_root():
    return app.dependency_overrides[protocol_data_root]()


def test_day2_start_and_agent_execution_read_apis(client):
    project = _create_project(
        client,
        _zip_bytes(
            {
                "src/Vault.sol": VULNERABLE_SOURCE,
                "src/OracleRouter.sol": "contract OracleRouter {}",
            }
        ),
    ).json()
    project_id = project["project_id"]
    bootstrap_local_simulator_routing_capacity(_protocol_root())
    prepared = client.post(f"/projects/{project_id}/prepare")
    assert prepared.status_code == 200
    assert client.get(f"/projects/{project_id}/status").json()["status"] == "ready"

    created = client.post(
        f"/projects/{project_id}/audit-runs",
        json={"execution_mode": "local_simulator"},
    )
    assert created.status_code == 201
    run_id = created.json()["audit_run_id"]
    started = client.post(f"/projects/{project_id}/audit-runs/{run_id}/start")
    assert started.status_code == 200
    run = started.json()
    assert run["status"] == "RUNNING"
    assert run["current_stage"] == "EXECUTING_AGENTS"
    states = {item["stage"]: item for item in run["stage_states"]}
    assert states["ROUTING"]["status"] == "COMPLETED"
    assert states["EXECUTING_AGENTS"]["status"] == "COMPLETED"
    assert states["REPRODUCING"]["status"] == "PENDING"
    assert run["routing_id"]

    listing = client.get(
        f"/projects/{project_id}/audit-runs/{run_id}/agent-executions"
    )
    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] == 2
    assert [item["category"] for item in body["agent_executions"]] == [
        "access_control",
        "reentrancy",
    ]
    assert all(item["operator_id"] for item in body["agent_executions"])
    assert all(item["finding_ids"] for item in body["agent_executions"])
    assert all(item["submission_ids"] for item in body["agent_executions"])
    assert all(item["agent_version"].endswith("_v1") for item in body["agent_executions"])
    execution_id = body["agent_executions"][0]["agent_execution_id"]
    detail = client.get(
        f"/projects/{project_id}/audit-runs/{run_id}/agent-executions/{execution_id}"
    )
    assert detail.status_code == 200
    assert detail.json()["agent_execution_id"] == execution_id
    assert "/home/" not in listing.text
    assert "/tmp/" not in listing.text

    repeated = client.post(f"/projects/{project_id}/audit-runs/{run_id}/start")
    assert repeated.status_code == 200
    assert repeated.json()["latest_event_sequence"] == run["latest_event_sequence"]
    assert client.get(
        f"/projects/{project_id}/audit-runs/{run_id}/agent-executions"
    ).json()["total"] == 2

    second_project = _create_project(client).json()["project_id"]
    assert client.get(
        f"/projects/{second_project}/audit-runs/{run_id}/agent-executions/{execution_id}"
    ).status_code == 404


def test_public_clients_cannot_select_agent_or_execution_identity(client):
    project_id = _create_project(client).json()["project_id"]
    for payload in (
        {"execution_mode": "local_simulator", "agent_class": "Anything"},
        {"execution_mode": "local_simulator", "agent_execution_id": "forged"},
        {"execution_mode": "local_simulator", "routing_id": "forged"},
    ):
        response = client.post(
            f"/projects/{project_id}/audit-runs", json=payload
        )
        assert response.status_code == 422
