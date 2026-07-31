import pytest


def _payload(**updates):
    payload = {
        "node_type": "agent",
        "display_name": "access-control-agent-01",
        "operator_id": "operator-local-01",
        "public_key": None,
        "supported_categories": ["Access Control"],
        "description": "Local access-control analysis agent.",
    }
    payload.update(updates)
    return payload


def _register(client, **updates):
    response = client.post("/nodes", json=_payload(**updates))
    assert response.status_code == 201
    return response.json()


def test_post_nodes_registers_agent_and_returns_201(client):
    node = _register(client)
    assert node["node_type"] == "agent"
    assert node["supported_categories"] == ["access_control"]
    assert node["status"] == "active"
    assert node["reputation_score"] == 0.5


def test_post_nodes_registers_validator_without_categories(client):
    node = _register(
        client,
        node_type="validator",
        display_name="general-validator-01",
        supported_categories=[],
    )
    assert node["node_type"] == "validator"
    assert node["supported_categories"] == []


def test_get_nodes_lists_and_filters_nodes(client):
    agent = _register(client)
    validator = _register(
        client,
        node_type="validator",
        display_name="reentrancy-validator",
        operator_id="operator-local-02",
        supported_categories=["reentrancy"],
    )
    client.post(
        f"/nodes/{validator['node_id']}/status",
        json={"status": "suspended", "reason": "Review."},
    )

    assert len(client.get("/nodes").json()) == 2
    assert [item["node_id"] for item in client.get("/nodes?node_type=agent").json()] == [agent["node_id"]]
    assert [item["node_id"] for item in client.get("/nodes?status=suspended").json()] == [
        validator["node_id"]
    ]
    assert [item["node_id"] for item in client.get("/nodes?category=Access%20Control").json()] == [
        agent["node_id"]
    ]


def test_get_node_and_missing_node(client):
    node = _register(client)
    assert client.get(f"/nodes/{node['node_id']}").json() == node
    assert client.get("/nodes/00000000-0000-0000-0000-000000000000").status_code == 404


def test_patch_updates_allowed_metadata(client):
    node = _register(client)
    response = client.patch(
        f"/nodes/{node['node_id']}",
        json={"display_name": "renamed-agent", "supported_categories": ["Reentrancy"]},
    )
    assert response.status_code == 200
    assert response.json()["display_name"] == "renamed-agent"
    assert response.json()["supported_categories"] == ["reentrancy"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reputation_score", 0.99),
        ("statistics", {"total_submissions": 5}),
        ("node_type", "hybrid"),
        ("operator_id", "different-operator"),
        ("status", "inactive"),
    ],
)
def test_patch_rejects_protected_fields(client, field, value):
    node = _register(client)
    response = client.patch(f"/nodes/{node['node_id']}", json={field: value})
    assert response.status_code == 422


def test_status_endpoint_suspends_and_reactivates(client):
    node = _register(client)
    suspended = client.post(
        f"/nodes/{node['node_id']}/status",
        json={"status": "suspended", "reason": "Administrative review."},
    )
    assert suspended.status_code == 200
    assert suspended.json()["status"] == "suspended"
    assert client.get(f"/nodes/{node['node_id']}").json()["status"] == "suspended"

    active = client.post(
        f"/nodes/{node['node_id']}/status",
        json={"status": "active", "reason": "Review completed."},
    )
    assert active.status_code == 200
    assert active.json()["status"] == "active"


def test_banned_node_cannot_be_reactivated(client):
    node = _register(client)
    client.post(
        f"/nodes/{node['node_id']}/status",
        json={"status": "banned", "reason": "Permanent administrative ban."},
    )
    response = client.post(
        f"/nodes/{node['node_id']}/status",
        json={"status": "active", "reason": "Attempted reactivation."},
    )
    assert response.status_code == 409


def test_duplicate_public_key_returns_409_but_null_is_reusable(client):
    _register(client, public_key="opaque-public-key")
    response = client.post(
        "/nodes",
        json=_payload(display_name="second-agent", public_key="opaque-public-key"),
    )
    assert response.status_code == 409
    _register(client, display_name="third-agent", public_key=None)
    _register(client, display_name="fourth-agent", public_key=None)


def test_api_never_exposes_paths_or_private_key_fields(client):
    response = client.post("/nodes", json=_payload())
    assert response.status_code == 201
    assert "/home/" not in response.text
    assert "data/protocol" not in response.text
    assert "private_key" not in response.json()

    rejected = client.post("/nodes", json={**_payload(), "private_key": "secret"})
    assert rejected.status_code == 422
