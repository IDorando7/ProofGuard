from app.core.paths import protocol_data_root
from app.main import app
from app.schemas.node import NodeCreate
from app.schemas.subnet import SubnetCreate
from app.services.node_registry_service import create_node
from app.services.subnet_registry_service import create_subnet
from tests.test_project_creation import _create_project
from tests.test_subnet_router_service import _member


def _root():
    return app.dependency_overrides[protocol_data_root]()


def _setup(client):
    project = _create_project(client).json()
    root = _root()
    subnet = create_subnet(root, SubnetCreate(category="access_control"))
    active = create_node(
        root,
        NodeCreate(
            node_type="agent",
            display_name="Routing Active",
            operator_id="operator-routing-active",
            supported_categories=["access_control"],
        ),
    )
    candidate = create_node(
        root,
        NodeCreate(
            node_type="agent",
            display_name="Routing Candidate",
            operator_id="operator-routing-candidate",
            supported_categories=["access_control"],
        ),
    )
    _member(root, subnet, active, "active", [90] * 6)
    _member(root, subnet, candidate, "candidate")
    return project["project_id"], subnet, active, candidate


def test_calculate_read_list_latest_and_unchanged_status_codes(client):
    project_id, _, _, _ = _setup(client)
    endpoint = f"/projects/{project_id}/routing/calculate"
    payload = {
        "categories": ["access_control"],
        "nodes_per_category": 2,
        "include_exploration": True,
        "allow_partial": True,
    }
    first = client.post(endpoint, json=payload)
    assert first.status_code == 201
    record = first.json()["record"]
    assert record["production_assignments"] == 1
    assert record["shadow_assignments"] == 1
    assert client.post(endpoint, json=payload).status_code == 200
    routing_id = record["routing_id"]
    assert client.get(
        f"/projects/{project_id}/routing/{routing_id}"
    ).json() == record
    assert client.get(
        f"/projects/{project_id}/routing"
    ).json()["total"] == 1
    assert client.get(
        f"/projects/{project_id}/routing/latest"
    ).json()["routing_id"] == routing_id


def test_categories_null_uses_scope_and_partial_results_are_explainable(client):
    project_id, _, _, _ = _setup(client)
    response = client.post(
        f"/projects/{project_id}/routing/calculate",
        json={"categories": None, "nodes_per_category": 2},
    )
    assert response.status_code == 201
    body = response.json()["record"]
    assert body["requested_categories"] == [
        "access_control",
        "reentrancy",
    ]
    reentrancy = body["results"][1]
    assert reentrancy["shortage_reasons"] == ["no_subnet"]
    assert reentrancy["warnings"]


def test_out_of_scope_and_client_controlled_assignment_fields_fail(client):
    project_id, _, _, _ = _setup(client)
    endpoint = f"/projects/{project_id}/routing/calculate"
    assert client.post(
        endpoint, json={"categories": ["governance"]}
    ).status_code == 400
    for payload in (
        {"selected_node_ids": ["node-1"]},
        {"assignment_mode": "production"},
        {"selection_type": "ranked"},
        {"category_score": 1},
        {"routing_status": "finalized"},
    ):
        assert client.post(endpoint, json=payload).status_code == 422


def test_finalize_and_usage_endpoints_are_idempotent(client):
    project_id, subnet, active, candidate = _setup(client)
    calculated = client.post(
        f"/projects/{project_id}/routing/calculate",
        json={"categories": ["access_control"], "nodes_per_category": 2},
    ).json()["record"]
    endpoint = (
        f"/projects/{project_id}/routing/"
        f"{calculated['routing_id']}/finalize"
    )
    first = client.post(endpoint)
    assert first.status_code == 200
    assert first.json()["usage_events_created"] == 2
    repeat = client.post(endpoint)
    assert repeat.status_code == 200
    assert repeat.json()["status"] == "already_finalized"
    assert repeat.json()["usage_events_existing"] == 2
    node_usage = client.get(f"/nodes/{candidate.node_id}/routing-usage")
    assert node_usage.status_code == 200
    assert node_usage.json()[0]["assignment_mode"] == "shadow"
    subnet_usage = client.get(
        f"/subnets/{subnet.subnet_id}/routing-usage"
    )
    assert subnet_usage.status_code == 200
    assert len(subnet_usage.json()) == 2
    assert client.get(
        f"/nodes/{active.node_id}/routing-usage?selection_type=ranked"
    ).status_code == 200


def test_missing_route_and_no_direct_assignment_editing(client):
    project_id, _, _, _ = _setup(client)
    assert client.get(
        f"/projects/{project_id}/routing/missing-route"
    ).status_code == 404
    assert client.patch(
        "/routing/missing-route",
        json={"assignments": ["node-1"]},
    ).status_code in {404, 405}
    assert client.post(
        "/nodes/node-1/force-route",
        json={"assignment_mode": "production"},
    ).status_code in {404, 405}


def test_routing_responses_expose_no_host_paths_or_execution_payloads(client):
    project_id, _, _, _ = _setup(client)
    response = client.post(
        f"/projects/{project_id}/routing/calculate",
        json={"categories": ["access_control"], "nodes_per_category": 2},
    )
    text = response.text.lower()
    assert response.status_code == 201
    for forbidden in (
        "/home/",
        "private_key",
        "benchmark_answers",
        "poc_content",
        "execution_result",
        '"stake"',
    ):
        assert forbidden not in text
