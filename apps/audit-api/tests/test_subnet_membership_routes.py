from app.core.paths import protocol_data_root
from app.main import app
from app.schemas.node import NodeCreate
from app.schemas.subnet import SubnetCreate
from app.services.category_performance_service import save_category_performance
from app.services.category_scoring_service import rebuild_category_score
from app.services.node_registry_service import create_node
from app.services.subnet_registry_service import create_subnet
from tests.test_category_scoring_service import _performance


def _root():
    return app.dependency_overrides[protocol_data_root]()


def _setup(root, name="membership-route-node", scores=None):
    subnet = create_subnet(
        root,
        SubnetCreate(
            category="access_control",
            maximum_active_nodes=1,
        ),
    )
    node = create_node(
        root,
        NodeCreate(
            node_type="agent",
            display_name=name,
            operator_id=f"operator-{name}",
            supported_categories=["access_control"],
        ),
    )
    if scores is not None:
        save_category_performance(
            root,
            _performance(
                node_id=node.node_id,
                accepted_scores=scores,
            ),
        )
        rebuild_category_score(root, node.node_id, "access_control")
    return subnet, node


def test_refresh_list_get_history_and_filters(client):
    root = _root()
    subnet, node = _setup(root, scores=[80] * 5)
    refresh = client.post(f"/subnets/{subnet.subnet_id}/members/refresh")
    assert refresh.status_code == 200
    body = refresh.json()
    assert body["evaluated_nodes"] == 1
    assert body["active_members"] == 1
    assert body["results"][0]["decision"]["reason_codes"] == ["eligible_active"]
    member_url = f"/subnets/{subnet.subnet_id}/members/{node.node_id}"
    member = client.get(member_url)
    assert member.status_code == 200
    assert member.json()["rank"] is None
    assert client.get(
        f"/subnets/{subnet.subnet_id}/members?status=active"
        "&minimum_category_score=0.6&minimum_finalized_submissions=5"
    ).json()["total"] == 1
    history = client.get(f"{member_url}/history")
    assert history.status_code == 200
    assert len(history.json()) == 1


def test_refresh_missing_sources_creates_candidate_without_dependency_records(client):
    root = _root()
    subnet, node = _setup(root)
    response = client.post(
        f"/subnets/{subnet.subnet_id}/members/refresh"
    )
    assert response.status_code == 200
    assert response.json()["candidate_members"] == 1
    assert response.json()["results"][0]["node_id"] == node.node_id
    assert not (root / "category-performance").exists()
    assert not (root / "category-scores").exists()


def test_evaluate_one_respects_full_capacity(client):
    root = _root()
    subnet, weaker = _setup(root, "membership-route-weaker", [80] * 5)
    stronger = create_node(
        root,
        NodeCreate(
            node_type="agent",
            display_name="Membership Route Expert",
            operator_id="operator-route-expert",
            supported_categories=["access_control"],
        ),
    )
    save_category_performance(
        root,
        _performance(
            node_id=stronger.node_id,
            accepted_scores=[100] * 10,
        ),
    )
    rebuild_category_score(root, stronger.node_id, "access_control")
    response = client.post(
        f"/subnets/{subnet.subnet_id}/members/{weaker.node_id}/evaluate"
    )
    assert response.status_code == 200
    assert response.json()["current_member"]["status"] == "probation"
    assert response.json()["decision"]["capacity_adjusted"] is True


def test_administrative_suspend_remove_and_idempotency(client):
    root = _root()
    subnet, node = _setup(root, scores=[100] * 10)
    base = f"/subnets/{subnet.subnet_id}/members/{node.node_id}"
    client.post(f"/subnets/{subnet.subnet_id}/members/refresh")
    suspended = client.post(
        f"{base}/suspend", json={"reason": "Manual security review."}
    )
    assert suspended.status_code == 200
    assert suspended.json()["current_member"]["status"] == "suspended"
    assert client.post(
        f"{base}/suspend", json={"reason": "Manual security review."}
    ).json()["status"] == "unchanged"
    removed = client.post(
        f"{base}/remove",
        json={"reason": "Node no longer participates."},
    )
    assert removed.status_code == 200
    assert removed.json()["current_member"]["status"] == "removed"


def test_missing_resources_and_public_status_control_are_rejected(client):
    root = _root()
    subnet, node = _setup(root)
    assert client.get(
        f"/subnets/{subnet.subnet_id}/members/{node.node_id}"
    ).status_code == 404
    assert client.post(
        f"/subnets/{subnet.subnet_id}/members/{node.node_id}/suspend",
        json={"reason": "ok"},
    ).status_code == 422
    for payload in (
        {"status": "active"},
        {"category_score": 1},
        {"rank": 1},
        {"weights": {"precision_weight": 1}},
    ):
        assert client.post(
            f"/subnets/{subnet.subnet_id}/members/refresh",
            json=payload,
        ).status_code == 422
    assert client.post(
        f"/subnets/{subnet.subnet_id}/members/{node.node_id}/status",
        json={"status": "expert"},
    ).status_code in {404, 405}


def test_api_response_has_no_sensitive_or_future_output(client):
    root = _root()
    subnet, _ = _setup(root, scores=[80] * 5)
    response = client.post(
        f"/subnets/{subnet.subnet_id}/members/refresh"
    )
    text = response.text.lower()
    assert response.status_code == 200
    for forbidden in (
        "/home/",
        "private_key",
        "routing_assignment",
        "reward_allocation",
        '"rank":1',
        '"stake"',
    ):
        assert forbidden not in text
