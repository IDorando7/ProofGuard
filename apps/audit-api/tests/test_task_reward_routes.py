from app.core.paths import protocol_data_root
from app.main import app
from tests.test_project_creation import _create_project
from tests.test_subnet_reward_routes import _setup


def _root():
    return app.dependency_overrides[protocol_data_root]()


def test_create_retry_read_by_id_read_by_routing_list_and_finalize(client):
    project_id, _, _, routing, _ = _setup(client)
    endpoint = f"/projects/{project_id}/task-reward-budgets"
    payload = {
        "routing_id": routing.routing_id,
        "total_budget_points": "10000.000000",
    }
    created = client.post(endpoint, json=payload)
    assert created.status_code == 201
    body = created.json()
    assert body["status"] == "created"
    budget = body["budget"]
    assert budget["reward_domain"] == "client_task"
    assert budget["reward_unit"] == "protocol_points"
    assert budget["total_budget_points"] == "10000.000000"
    assert budget["miner_pool_points"] == "7000.000000"
    assert budget["validator_pool_points"] == "2000.000000"
    assert budget["protocol_pool_points"] == "1000.000000"
    assert budget["status"] == "draft"
    assert budget["finalized_at"] is None

    repeated = client.post(endpoint, json=payload)
    assert repeated.status_code == 200
    assert repeated.json()["status"] == "unchanged"
    assert repeated.json()["budget"]["task_reward_budget_id"] == budget["task_reward_budget_id"]

    by_id = client.get(f"{endpoint}/{budget['task_reward_budget_id']}")
    assert by_id.status_code == 200
    assert by_id.json() == budget
    by_routing = client.get(
        f"/projects/{project_id}/routing/{routing.routing_id}/task-reward-budget"
    )
    assert by_routing.status_code == 200
    assert by_routing.json() == budget
    listed = client.get(endpoint)
    assert listed.status_code == 200
    assert listed.json() == [budget]

    finalized = client.post(
        f"{endpoint}/{budget['task_reward_budget_id']}/finalize"
    )
    assert finalized.status_code == 200
    assert finalized.json()["status"] == "finalized"
    assert finalized.json()["budget"]["status"] == "finalized"
    assert finalized.json()["budget"]["finalized_at"] is not None
    assert client.post(
        f"{endpoint}/{budget['task_reward_budget_id']}/finalize"
    ).json()["status"] == "already_finalized"


def test_project_mismatch_conflict_missing_routing_and_invalid_budget(client):
    project_id, _, _, routing, _ = _setup(client)
    other_project = _create_project(client).json()["project_id"]
    mismatch = client.post(
        f"/projects/{other_project}/task-reward-budgets",
        json={
            "routing_id": routing.routing_id,
            "total_budget_points": "100.000000",
        },
    )
    assert mismatch.status_code == 409
    missing = client.post(
        f"/projects/{project_id}/task-reward-budgets",
        json={
            "routing_id": "routing-missing",
            "total_budget_points": "100.000000",
        },
    )
    assert missing.status_code == 404
    invalid = client.post(
        f"/projects/{project_id}/task-reward-budgets",
        json={
            "routing_id": routing.routing_id,
            "total_budget_points": "0.000000",
        },
    )
    assert invalid.status_code == 422


def test_conflicting_budget_and_invalid_share_override_fail(client):
    project_id, _, _, routing, _ = _setup(client)
    endpoint = f"/projects/{project_id}/task-reward-budgets"
    base = {
        "routing_id": routing.routing_id,
        "total_budget_points": "100.000000",
    }
    assert client.post(endpoint, json=base).status_code == 201
    conflict = client.post(
        endpoint,
        json={**base, "total_budget_points": "101.000000"},
    )
    assert conflict.status_code == 409
    invalid_split = client.post(
        endpoint,
        json={
            **base,
            "pool_split": {
                "miner_share": "0.8",
                "validator_share": "0.3",
                "protocol_share": "0.1",
            },
        },
    )
    assert invalid_split.status_code == 422


def test_clients_cannot_forge_derived_pools_events_or_payment_fields(client):
    project_id, _, _, routing, _ = _setup(client)
    endpoint = f"/projects/{project_id}/task-reward-budgets"
    for field in (
        "task_reward_budget_id",
        "miner_pool_points",
        "validator_pool_points",
        "protocol_pool_points",
        "source_fingerprint",
        "finalized_at",
        "reward_events",
        "wallet_address",
        "token_address",
    ):
        response = client.post(
            endpoint,
            json={
                "routing_id": routing.routing_id,
                "total_budget_points": "100.000000",
                field: "forged",
            },
        )
        assert response.status_code == 422


def test_task_budget_api_has_no_payout_payment_or_host_path_side_effects(client):
    project_id, _, _, routing, _ = _setup(client)
    root = _root()
    response = client.post(
        f"/projects/{project_id}/task-reward-budgets",
        json={
            "routing_id": routing.routing_id,
            "total_budget_points": "100.000001",
        },
    )
    assert response.status_code == 201
    text = response.text.lower()
    for forbidden in (
        "/home/",
        "private_key",
        "wallet_address",
        "token_transfer",
        "blockchain_transaction",
    ):
        assert forbidden not in text
    assert not list((root / "rewards" / "events").glob("**/*.json"))
    assert not list((root / "subnet-rewards" / "events").glob("**/*.json"))
    for path in (
        "/pay",
        "/transfer-token",
        "/reward-node",
        "/set-node-reward",
    ):
        assert client.post(path, json={}).status_code in {404, 405}
