from app.core.paths import protocol_data_root
from app.main import app
from tests.test_project_creation import _create_project
from tests.test_subnet_reward_routes import _setup


def _root():
    return app.dependency_overrides[protocol_data_root]()


def test_rebuild_list_read_and_submission_lookup_routes(client):
    project_id, _, _, routing, submission = _setup(client)
    base = (
        f"/projects/{project_id}/routing/{routing.routing_id}/finding-clusters"
    )
    rebuilt = client.post(f"{base}/rebuild", json={})
    assert rebuilt.status_code == 200
    body = rebuilt.json()
    assert body["total_clusters"] == 1
    cluster = body["clusters"][0]
    assert cluster["report_count"] == 1
    assert cluster["distinct_operator_count"] == 1
    assert "/home/" not in rebuilt.text

    repeated = client.post(f"{base}/rebuild", json={})
    assert repeated.status_code == 200
    assert repeated.json()["unchanged_clusters"] == 1
    assert repeated.json()["clusters"] == body["clusters"]

    listed = client.get(base)
    assert listed.status_code == 200
    assert listed.json()["clusters"] == body["clusters"]
    read = client.get(f"{base}/{cluster['finding_cluster_id']}")
    assert read.status_code == 200
    assert read.json() == cluster
    lookup = client.get(
        f"/submissions/{submission.submission_id}/finding-cluster"
    )
    assert lookup.status_code == 200
    assert lookup.json()["finding_cluster"] == cluster
    finalized = client.post(f"{base}/finalize", json={})
    assert finalized.status_code == 200
    assert finalized.json()["finalized_clusters"] == 1
    assert finalized.json()["clusters"][0]["status"] == "finalized"
    assert client.post(f"{base}/finalize", json={}).json()[
        "already_finalized_clusters"
    ] == 1
    assert client.get(f"{base}/finding_cluster_{'0' * 64}").status_code == 404


def test_clients_cannot_set_cluster_members_canonical_severity_or_rewards(client):
    project_id, _, _, routing, _ = _setup(client)
    endpoint = (
        f"/projects/{project_id}/routing/{routing.routing_id}/finding-clusters/rebuild"
    )
    for field in (
        "members",
        "canonical_finding_id",
        "final_severity",
        "reward_amount",
        "wallet_address",
        "private_key",
    ):
        assert client.post(endpoint, json={field: "forged"}).status_code == 422
    assert client.post("/finding-clusters/manual", json={}).status_code in {404, 405}


def test_wrong_project_routing_relationship_fails(client):
    project_id, _, _, routing, _ = _setup(client)
    other_project = _create_project(client).json()["project_id"]
    response = client.post(
        f"/projects/{other_project}/routing/{routing.routing_id}/finding-clusters/rebuild",
        json={},
    )
    assert response.status_code in {404, 409}
    assert project_id != other_project
