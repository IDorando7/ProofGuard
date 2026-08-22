from app.core.paths import protocol_data_root
from app.main import app
from tests.test_task_operator_reward_routes import _operator_ready


def _root():
    return app.dependency_overrides[protocol_data_root]()


def test_create_calculate_finalize_read_and_history_routes(client):
    project_id, routing, day4 = _operator_ready(client)
    create_url = (
        f"/projects/{project_id}/routing/{routing.routing_id}"
        "/task-reward-cycles"
    )
    created = client.post(
        create_url,
        json={"task_reward_budget_id": day4["task_reward_budget_id"]},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["processing_status"] == "created"
    cycle = body["cycle"]
    assert cycle["status"] == "draft"
    assert cycle["reward_event_count"] == 0

    repeated = client.post(
        create_url,
        json={"task_reward_budget_id": day4["task_reward_budget_id"]},
    )
    assert repeated.status_code == 200
    assert repeated.json()["processing_status"] == "unchanged"

    cycle_id = cycle["reward_cycle_id"]
    calculated = client.post(
        f"/projects/{project_id}/task-reward-cycles/{cycle_id}/calculate",
        json={},
    )
    assert calculated.status_code == 200
    assert calculated.json()["cycle"]["status"] == "calculated"
    assert calculated.json()["cycle"]["reward_event_count"] == 0
    calculated_verification = client.get(
        f"/projects/{project_id}/task-reward-cycles/{cycle_id}/verify"
    )
    assert calculated_verification.status_code == 200
    assert calculated_verification.json()["verification_status"] == (
        "calculated_not_finalized"
    )
    assert calculated_verification.json()["safe_retry_finalize"] is True

    finalized = client.post(
        f"/projects/{project_id}/task-reward-cycles/{cycle_id}/finalize",
        json={},
    )
    assert finalized.status_code == 200
    final_cycle = finalized.json()["cycle"]
    assert final_cycle["status"] == "finalized"
    assert final_cycle["reward_event_count"] == 1
    assert (
        final_cycle["distributed_miner_points"]
        == day4["miner_pool_points"]
    )
    finalized_verification = client.get(
        f"/projects/{project_id}/task-reward-cycles/{cycle_id}/verify"
    )
    assert finalized_verification.status_code == 200
    assert finalized_verification.json()["verification_status"] == "clean_finalized"
    assert finalized_verification.json()["event_set_complete"] is True

    assert client.get(
        f"/projects/{project_id}/task-reward-cycles/{cycle_id}"
    ).json() == final_cycle
    assert client.get(
        f"/projects/{project_id}/routing/{routing.routing_id}/task-reward-cycle"
    ).json() == final_cycle
    assert client.get(
        f"/projects/{project_id}/task-reward-cycles"
    ).json()["total"] == 1
    events = client.get(
        f"/projects/{project_id}/task-reward-cycles/{cycle_id}/events"
    ).json()
    assert events["total"] == 1
    event = events["events"][0]
    assert client.get(
        f"/operators/{event['operator_id']}/task-rewards"
    ).json()["total_reward_events"] == 1
    assert client.get(
        f"/nodes/{event['node_id']}/task-rewards"
    ).json()["total_reward_events"] == 1
    assert client.get(
        f"/submissions/{event['submission_id']}/task-rewards"
    ).json()["total_reward_events"] == 1
    assert client.get(
        f"/finding-clusters/{event['finding_cluster_id']}/task-rewards"
    ).json()["total_reward_events"] == 1

    final_again = client.post(
        f"/projects/{project_id}/task-reward-cycles/{cycle_id}/finalize",
        json={},
    )
    assert final_again.status_code == 200
    assert final_again.json()["processing_status"] == "already_finalized"
    assert final_again.json()["reward_events_created"] == 0


def test_verify_unknown_cycle_returns_404(client):
    project_id, _, _ = _operator_ready(client)
    response = client.get(
        f"/projects/{project_id}/task-reward-cycles/unknown-cycle/verify"
    )
    assert response.status_code == 404


def test_api_rejects_client_rewards_chief_events_and_finalized_mutation(client):
    project_id, routing, day4 = _operator_ready(client)
    endpoint = (
        f"/projects/{project_id}/routing/{routing.routing_id}"
        "/task-reward-cycles"
    )
    for field in (
        "total_reward_points",
        "operator_id",
        "chief_finder",
        "reward_event_id",
        "quality_rank",
        "status",
    ):
        response = client.post(
            endpoint,
            json={
                "task_reward_budget_id": day4["task_reward_budget_id"],
                field: "forged",
            },
        )
        assert response.status_code == 422
    created = client.post(
        endpoint,
        json={"task_reward_budget_id": day4["task_reward_budget_id"]},
    ).json()["cycle"]
    cycle_id = created["reward_cycle_id"]
    for action in ("calculate", "finalize"):
        response = client.post(
            f"/projects/{project_id}/task-reward-cycles/{cycle_id}/{action}",
            json={"total_reward_points": "999999.000000"},
        )
        assert response.status_code == 422
    assert not (_root() / "task-rewards" / "events" / "manual").exists()
