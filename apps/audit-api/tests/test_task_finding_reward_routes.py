from tests.test_subnet_reward_routes import _setup


def _ready(client):
    project_id, _, _, routing, _ = _setup(client)
    clusters_endpoint = (
        f"/projects/{project_id}/routing/{routing.routing_id}/finding-clusters"
    )
    assert client.post(f"{clusters_endpoint}/rebuild", json={}).status_code == 200
    finalized_clusters = client.post(f"{clusters_endpoint}/finalize", json={})
    assert finalized_clusters.status_code == 200
    assert finalized_clusters.json()["finalized_clusters"] == 1
    repeated_finalization = client.post(f"{clusters_endpoint}/finalize", json={})
    assert repeated_finalization.status_code == 200
    assert repeated_finalization.json()["already_finalized_clusters"] == 1
    budget_response = client.post(
        f"/projects/{project_id}/task-reward-budgets",
        json={
            "routing_id": routing.routing_id,
            "total_budget_points": "10000.000000",
        },
    )
    assert budget_response.status_code == 201
    budget = budget_response.json()["budget"]
    finalized = client.post(
        f"/projects/{project_id}/task-reward-budgets/{budget['task_reward_budget_id']}/finalize"
    )
    assert finalized.status_code == 200
    return project_id, routing, finalized.json()["budget"]


def test_calculate_retry_read_latest_and_list(client):
    project_id, routing, budget = _ready(client)
    base = (
        f"/projects/{project_id}/routing/{routing.routing_id}"
        "/task-rewards/findings"
    )
    payload = {"task_reward_budget_id": budget["task_reward_budget_id"]}
    created = client.post(f"{base}/calculate", json=payload)
    assert created.status_code == 201
    body = created.json()
    assert body["processing_status"] == "calculated"
    calculation = body["calculation"]
    assert calculation["allocation_scope"] == "category_isolated"
    assert calculation["distributed_cluster_points"] == "7000.000000"
    assert calculation["undistributed_cluster_points"] == "0.000000"
    assert calculation["cluster_allocations"][0]["finding_score"] == "8.000000"
    assert "operator_reward" not in created.text
    assert "quality_score" not in created.text

    repeated = client.post(f"{base}/calculate", json=payload)
    assert repeated.status_code == 200
    assert repeated.json()["processing_status"] == "unchanged"
    calculation_id = calculation["calculation_id"]
    assert client.get(f"{base}/{calculation_id}").json() == calculation
    assert client.get(f"{base}/latest").json() == calculation
    listed = client.get(base)
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["calculations"] == [calculation]


def test_api_forbids_server_derived_values_and_wrong_budget(client):
    project_id, routing, budget = _ready(client)
    endpoint = (
        f"/projects/{project_id}/routing/{routing.routing_id}"
        "/task-rewards/findings/calculate"
    )
    for field in (
        "final_severity",
        "distinct_operator_count",
        "uniqueness",
        "finding_score",
        "cluster_reward_points",
        "quality_score",
        "top_k",
        "chief_finder",
    ):
        response = client.post(
            endpoint,
            json={"task_reward_budget_id": budget["task_reward_budget_id"], field: "1"},
        )
        assert response.status_code == 422
    wrong = client.post(
        endpoint, json={"task_reward_budget_id": "task_reward_budget_wrong"}
    )
    assert wrong.status_code == 409


def test_global_request_rejects_category_weights(client):
    project_id, routing, budget = _ready(client)
    endpoint = (
        f"/projects/{project_id}/routing/{routing.routing_id}"
        "/task-rewards/findings/calculate"
    )
    response = client.post(
        endpoint,
        json={
            "task_reward_budget_id": budget["task_reward_budget_id"],
            "allocation_scope": "global",
            "category_weights": {"access_control": "1"},
        },
    )
    assert response.status_code == 422
