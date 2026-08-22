from app.core.paths import protocol_data_root
from app.main import app
from app.schemas.node import NodeCreate
from app.services.finding_cluster_service import finalize_finding_clusters_for_task
from app.services.node_registry_service import create_node
from tests.test_subnet_reward_routes import _setup


def _root():
    return app.dependency_overrides[protocol_data_root]()


def _context(client):
    project_id, _, _, routing, _ = _setup(client)
    rebuilt = client.post(
        f"/projects/{project_id}/routing/{routing.routing_id}/finding-clusters/rebuild",
        json={},
    )
    cluster_id = rebuilt.json()["clusters"][0]["finding_cluster_id"]
    finalize_finding_clusters_for_task(_root(), project_id, routing.routing_id)
    for index in range(6):
        create_node(
            _root(),
            NodeCreate(
                node_type="validator",
                display_name=f"API Committee Validator {index}",
                operator_id=f"api-committee-operator-{index}",
                supported_categories=["access_control"],
            ),
        )
    base = f"/projects/{project_id}/routing/{routing.routing_id}"
    return project_id, routing, cluster_id, base


def test_committee_plan_read_list_finalize_and_assignment_api(client):
    _, routing, cluster_id, base = _context(client)
    endpoint = f"{base}/finding-clusters/{cluster_id}/validator-committees"
    planned = client.post(endpoint, json={})
    assert planned.status_code == 201
    committee = planned.json()
    assert committee["assurance_mode"] == "standard"
    assert len(committee["authoritative_seats"]) == 5
    committee_id = committee["validator_committee_id"]
    assert client.get(f"{base}/validator-committees/{committee_id}").json() == committee
    listed = client.get(endpoint)
    assert listed.status_code == 200
    assert listed.json()["committees"] == [committee]
    finalized = client.post(
        f"{base}/validator-committees/{committee_id}/finalize", json={}
    )
    assert finalized.status_code == 200
    assert finalized.json()["committee"]["status"] == "finalized"
    assignments = client.get(
        f"{base}/validator-committees/{committee_id}/assignments"
    )
    assert assignments.status_code == 200
    assert assignments.json()["total"] == 6
    assert [item["assignment_role"] for item in assignments.json()["assignments"]] == [
        "authoritative",
        "authoritative",
        "authoritative",
        "authoritative",
        "authoritative",
        "shadow",
    ]
    assert all(item["validator_committee_id"] == committee_id for item in assignments.json()["assignments"])


def test_public_committee_api_rejects_member_and_assurance_forgery(client):
    _, _, cluster_id, base = _context(client)
    endpoint = f"{base}/finding-clusters/{cluster_id}/validator-committees"
    for payload in (
        {"validator_node_ids": ["friendly-validator"]},
        {"operator_ids": ["friendly-operator"]},
        {"category": "reentrancy"},
        {"reporting_operator_ids": []},
        {"assurance_mode": "high_assurance"},
        {"private_key": "secret"},
    ):
        assert client.post(endpoint, json=payload).status_code == 422


def test_committee_creation_has_no_reproduction_attestation_consensus_or_reward(client):
    _, routing, cluster_id, base = _context(client)
    planned = client.post(
        f"{base}/finding-clusters/{cluster_id}/validator-committees", json={}
    ).json()
    response = client.post(
        f"{base}/validator-committees/{planned['validator_committee_id']}/finalize",
        json={},
    )
    assert response.status_code == 200
    root = _root()
    assert not (root / "validator-protocol" / "reproductions" / routing.routing_id).exists()
    assert not (root / "validator-protocol" / "attestations" / routing.routing_id).exists()
    assert not (root / "validator-consensus").exists()
    assert not (root / "validator-rewards").exists()


def test_cross_project_and_unknown_committee_are_rejected(client):
    project_id, routing, _, base = _context(client)
    unknown = "validator_committee_" + "0" * 64
    assert client.get(f"{base}/validator-committees/{unknown}").status_code == 404
    # Existing project validation occurs before protocol relationship lookup.
    response = client.get(
        f"/projects/not-{project_id}/routing/{routing.routing_id}/validator-committees/{unknown}"
    )
    assert response.status_code == 404
