from app.core.paths import protocol_data_root
from app.main import app
from app.services.project_service import project_workspace
from app.services.report_quality_assessment_service import assess_report_quality
from tests.test_task_finding_reward_routes import _ready
from tests.test_task_operator_reward_service import _quality


def _root():
    return app.dependency_overrides[protocol_data_root]()


def _operator_ready(client):
    project_id, routing, budget = _ready(client)
    clusters = client.get(
        f"/projects/{project_id}/routing/{routing.routing_id}/finding-clusters"
    ).json()["clusters"]
    cluster = clusters[0]
    assess_report_quality(
        _root(),
        project_workspace(project_id),
        project_id,
        routing.routing_id,
        cluster["finding_cluster_id"],
        cluster["members"][0]["submission_id"],
        _quality("high"),
    )
    day4 = client.post(
        f"/projects/{project_id}/routing/{routing.routing_id}/task-rewards/findings/calculate",
        json={"task_reward_budget_id": budget["task_reward_budget_id"]},
    )
    assert day4.status_code == 201
    return project_id, routing, day4.json()["calculation"]


def test_calculate_retry_read_latest_and_list_preview(client):
    project_id, routing, day4 = _operator_ready(client)
    base = (
        f"/projects/{project_id}/routing/{routing.routing_id}"
        "/task-rewards/operators"
    )
    payload = {
        "task_finding_reward_calculation_id": day4["calculation_id"]
    }
    created = client.post(f"{base}/calculate", json=payload)
    assert created.status_code == 201
    body = created.json()
    assert body["processing_status"] == "calculated"
    calculation = body["calculation"]
    assert calculation["distributed_operator_points"] == "7000.000000"
    assert calculation["undistributed_cluster_points"] == "0.000000"
    allocation = calculation["cluster_payouts"][0]["operator_allocations"][0]
    assert allocation["quality_weight"]
    assert allocation["chief_finder"] is True
    assert "RewardEvent" in body["message"]
    assert "reward_event_id" not in created.text

    repeated = client.post(f"{base}/calculate", json=payload)
    assert repeated.status_code == 200
    assert repeated.json()["processing_status"] == "unchanged"
    calculation_id = calculation["calculation_id"]
    assert client.get(f"{base}/{calculation_id}").json() == calculation
    assert client.get(f"{base}/latest").json() == calculation
    listed = client.get(base)
    assert listed.status_code == 200
    assert listed.json()["total"] == 1


def test_api_rejects_client_ranking_identity_quality_chief_and_reward_fields(client):
    project_id, routing, day4 = _operator_ready(client)
    endpoint = (
        f"/projects/{project_id}/routing/{routing.routing_id}"
        "/task-rewards/operators/calculate"
    )
    for field in (
        "quality_score",
        "operator_id",
        "top_k_operator_ids",
        "quality_rank",
        "chief_operator_id",
        "chief_submission_id",
        "quality_weight",
        "total_reward_points",
        "rewarded",
    ):
        response = client.post(
            endpoint,
            json={
                "task_finding_reward_calculation_id": day4["calculation_id"],
                field: "forged",
            },
        )
        assert response.status_code == 422
    missing = client.post(
        endpoint,
        json={"task_finding_reward_calculation_id": "missing-calculation"},
    )
    assert missing.status_code == 404
