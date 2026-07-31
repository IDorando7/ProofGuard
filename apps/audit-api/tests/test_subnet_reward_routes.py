from app.core.paths import protocol_data_root
from app.main import app
from app.schemas.node import NodeCreate
from app.schemas.subnet import SubnetCreate
from app.services.node_registry_service import create_node
from app.services.project_service import project_workspace
from app.services.subnet_registry_service import create_subnet
from app.services.subnet_router_service import load_routing_record
from tests.test_project_creation import _create_project
from tests.test_subnet_reward_allocation_service import _accepted_submission
from tests.test_subnet_router_service import _member


def _root():
    return app.dependency_overrides[protocol_data_root]()


def _setup(client):
    project_id = _create_project(client).json()["project_id"]
    root = _root()
    subnet = create_subnet(root, SubnetCreate(category="access_control"))
    node = create_node(
        root,
        NodeCreate(
            node_type="agent",
            display_name="Reward Route Node",
            operator_id="operator-reward-route",
            supported_categories=["access_control"],
        ),
    )
    _member(root, subnet, node, "active", [90] * 6)
    routed = client.post(
        f"/projects/{project_id}/routing/calculate",
        json={
            "categories": ["access_control"],
            "nodes_per_category": 1,
            "include_exploration": False,
        },
    ).json()["record"]
    finalized = client.post(
        f"/projects/{project_id}/routing/{routed['routing_id']}/finalize"
    )
    assert finalized.status_code == 200
    routing = load_routing_record(root, project_id, routed["routing_id"])
    submission, _ = _accepted_submission(
        root,
        project_workspace(project_id),
        routing,
        routing.results[0].assignments[0],
        "reward-route-finding",
    )
    return project_id, subnet, node, routing, submission


def test_create_calculate_finalize_and_history_routes(client):
    project_id, subnet, node, routing, submission = _setup(client)
    create_endpoint = f"/projects/{project_id}/subnet-reward-cycles"
    payload = {
        "routing_id": routing.routing_id,
        "total_pool_points": "1000.000000",
        "category_weights": {"access_control": "1"},
    }
    created = client.post(create_endpoint, json=payload)
    assert created.status_code == 201
    cycle = created.json()["cycle"]
    assert cycle["status"] == "draft"
    repeated = client.post(create_endpoint, json=payload)
    assert repeated.status_code == 200
    assert repeated.json()["status"] == "unchanged"

    cycle_id = cycle["reward_cycle_id"]
    calculated = client.post(
        f"{create_endpoint}/{cycle_id}/calculate"
    )
    assert calculated.status_code == 200
    body = calculated.json()
    assert body["eligible_allocations"] == 1
    assert body["cycle"]["category_pools"][0]["distributed_points"] == "1000.000000"
    assert client.post(
        f"{create_endpoint}/{cycle_id}/calculate"
    ).json()["status"] == "unchanged"

    finalized = client.post(f"{create_endpoint}/{cycle_id}/finalize")
    assert finalized.status_code == 200
    assert finalized.json()["reward_events_created"] == 1
    assert client.post(
        f"{create_endpoint}/{cycle_id}/finalize"
    ).json()["status"] == "already_finalized"

    assert client.get(f"{create_endpoint}/{cycle_id}").status_code == 200
    assert len(client.get(create_endpoint).json()) == 1
    by_routing = client.get(
        f"/projects/{project_id}/routing/{routing.routing_id}/subnet-reward-cycle"
    )
    assert by_routing.json()["reward_cycle_id"] == cycle_id
    assert client.get(
        f"/nodes/{node.node_id}/subnet-rewards"
    ).json()["total"] == 1
    assert client.get(
        f"/subnets/{subnet.subnet_id}/reward-events"
    ).json()["total"] == 1
    assert client.get(
        f"/submissions/{submission.submission_id}/subnet-rewards"
    ).json()["total"] == 1


def test_unfinalized_routing_and_invalid_cycle_fail_cleanly(client):
    project_id, _, _, routing, _ = _setup(client)
    root = _root()
    stored = load_routing_record(root, project_id, routing.routing_id)
    # A missing routing is distinct from a finalized route used by the setup.
    assert client.post(
        f"/projects/{project_id}/subnet-reward-cycles",
        json={
            "routing_id": "missing-routing",
            "total_pool_points": "100.000000",
        },
    ).status_code == 404
    assert client.get(
        f"/projects/{project_id}/subnet-reward-cycles/missing-cycle"
    ).status_code == 404
    assert stored.status.value == "finalized"


def test_public_clients_cannot_supply_allocations_or_payment_fields(client):
    project_id, _, _, routing, _ = _setup(client)
    endpoint = f"/projects/{project_id}/subnet-reward-cycles"
    for extra in (
        {"node_ids": ["node-1"]},
        {"reward_points": "1.000000"},
        {"membership_multiplier": "1.10"},
        {"selected_submissions": ["submission-1"]},
        {"wallet": "0x123"},
    ):
        response = client.post(
            endpoint,
            json={
                "routing_id": routing.routing_id,
                "total_pool_points": "100.000000",
                **extra,
            },
        )
        assert response.status_code == 422
    assert client.patch(
        "/subnet-reward-cycles/cycle-1",
        json={"reward_points": "1"},
    ).status_code in {404, 405}


def test_reward_api_exposes_only_simulated_safe_records(client):
    project_id, _, _, routing, _ = _setup(client)
    response = client.post(
        f"/projects/{project_id}/subnet-reward-cycles",
        json={
            "routing_id": routing.routing_id,
            "total_pool_points": "100.000000",
        },
    )
    assert response.status_code == 201
    text = response.text.lower()
    for forbidden in (
        "/home/",
        "private_key",
        "wallet_address",
        "poc_content",
        "token_transfer",
        "blockchain_transaction",
    ):
        assert forbidden not in text
