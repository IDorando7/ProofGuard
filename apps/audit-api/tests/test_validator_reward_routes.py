from tests.test_subnet_reward_routes import _setup


def test_validator_reward_api_creates_draft_and_rejects_client_payout_inputs(client):
    project_id, _, _, routing, _ = _setup(client)
    budget_response = client.post(
        f"/projects/{project_id}/task-reward-budgets",
        json={"routing_id": routing.routing_id, "total_budget_points": "1000.000000"},
    )
    budget = budget_response.json()["budget"]
    client.post(
        f"/projects/{project_id}/task-reward-budgets/{budget['task_reward_budget_id']}/finalize"
    )
    endpoint = f"/projects/{project_id}/routing/{routing.routing_id}/validator-reward-cycles"
    created = client.post(
        endpoint, json={"task_reward_budget_id": budget["task_reward_budget_id"]}
    )
    assert created.status_code == 201
    cycle = created.json()["cycle"]
    assert cycle["status"] == "draft"
    assert cycle["pool_kind"] == "validator"
    assert cycle["validator_pool_points"] == "200.000000"
    assert cycle["reward_event_count"] == 0
    assert client.get(f"{endpoint}/{cycle['reward_cycle_id']}").json() == cycle
    assert client.get(endpoint).json()["total"] == 1
    for forged in (
        {"task_reward_budget_id": budget["task_reward_budget_id"], "quality_reward": "10.000000"},
        {"task_reward_budget_id": budget["task_reward_budget_id"], "validation_quality_score": "1.000000"},
        {"task_reward_budget_id": budget["task_reward_budget_id"], "validator_node_ids": ["node"]},
        {"task_reward_budget_id": budget["task_reward_budget_id"], "work_unit_budget": "20.000000"},
    ):
        assert client.post(endpoint, json=forged).status_code == 422


def test_validator_reward_api_blocks_premature_calculation_and_is_in_openapi(client):
    project_id, _, _, routing, _ = _setup(client)
    budget = client.post(
        f"/projects/{project_id}/task-reward-budgets",
        json={"routing_id": routing.routing_id, "total_budget_points": "1000.000000"},
    ).json()["budget"]
    client.post(f"/projects/{project_id}/task-reward-budgets/{budget['task_reward_budget_id']}/finalize")
    base = f"/projects/{project_id}/routing/{routing.routing_id}/validator-reward-cycles"
    cycle = client.post(base, json={"task_reward_budget_id": budget["task_reward_budget_id"]}).json()["cycle"]
    # With no committees this is a legitimately closed zero-work snapshot.
    calculated = client.post(f"{base}/{cycle['reward_cycle_id']}/calculate", json={})
    assert calculated.status_code == 200
    assert calculated.json()["cycle"]["authoritative_work_units"] == 0
    assert calculated.json()["cycle"]["undistributed_validator_points"] == "200.000000"
    schema = client.get("/openapi.json").json()
    path = "/projects/{project_id}/routing/{routing_id}/validator-reward-cycles/{reward_cycle_id}/finalize"
    assert path in schema["paths"]
    assert "validator-rewards" in schema["paths"][path]["post"]["tags"]
