from copy import deepcopy

import pytest

from app.core.paths import protocol_data_root
from app.main import app
from app.schemas.node import NodeCreate
from app.schemas.subnet import SubnetCreate
from app.services.category_performance_service import save_category_performance
from app.services.node_registry_service import create_node
from app.services.subnet_registry_service import (
    create_subnet,
    load_subnet,
)
from tests.test_category_scoring_service import _performance


def _root():
    return app.dependency_overrides[protocol_data_root]()


def _node(root, name="score-route-node"):
    return create_node(
        root,
        NodeCreate(
            node_type="agent",
            display_name=name,
            operator_id=f"operator-{name}",
            supported_categories=["access_control", "reentrancy"],
        ),
    )


def _store(root, node, category="access_control", scores=None):
    return save_category_performance(
        root,
        _performance(
            node_id=node.node_id,
            category=category,
            accepted_scores=scores or [90] * 5,
        ),
    )


def test_rebuild_read_and_repeat_are_explainable_and_idempotent(client):
    root = _root()
    node = _node(root)
    _store(root, node)
    endpoint = (
        f"/nodes/{node.node_id}/category-scores/access_control"
    )
    first = client.post(f"{endpoint}/rebuild")
    assert first.status_code == 200
    body = first.json()
    assert body["status"] == "calculated"
    record = body["record"]
    for field in (
        "components",
        "weights",
        "weighted_components",
        "penalties",
        "confidence_adjusted_score",
        "category_score",
        "score_band",
        "explanation",
    ):
        assert field in record
    assert client.post(f"{endpoint}/rebuild").json()["status"] == "unchanged"
    read = client.get(endpoint)
    assert read.status_code == 200
    assert read.json() == record


def test_node_and_global_rebuild_and_list_filters(client):
    root = _root()
    node = _node(root)
    _store(root, node, "access_control", [100] * 10)
    _store(root, node, "reentrancy", [60] * 3)
    node_rebuild = client.post(
        f"/nodes/{node.node_id}/category-scores/rebuild"
    )
    assert node_rebuild.status_code == 200
    assert [item["category"] for item in node_rebuild.json()] == [
        "access_control",
        "reentrancy",
    ]
    batch = client.post("/category-scores/rebuild")
    assert batch.status_code == 200
    assert batch.json()["total_records"] == 2
    assert batch.json()["unchanged_records"] == 2
    assert len(client.get(f"/nodes/{node.node_id}/category-scores").json()) == 2
    assert len(client.get("/category-scores").json()) == 2
    assert len(
        client.get(
            f"/category-scores?node_id={node.node_id}"
            "&category=access_control&minimum_score=0.8"
            "&minimum_confidence=1&score_band=expert"
        ).json()
    ) == 1


def test_subnet_scores_filter_exact_category_without_membership(client):
    root = _root()
    node = _node(root)
    _store(root, node, "access_control", [100] * 10)
    _store(root, node, "reentrancy", [100] * 10)
    client.post(f"/nodes/{node.node_id}/category-scores/rebuild")
    subnet = create_subnet(root, SubnetCreate(category="access_control"))
    snapshot = deepcopy(load_subnet(root, subnet.subnet_id))
    response = client.get(
        f"/subnets/{subnet.subnet_id}/category-scores"
    )
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["category"] == "access_control"
    assert "rank" not in response.json()[0]
    assert load_subnet(root, subnet.subnet_id) == snapshot
    assert not list((root / "subnets").glob("*/members/*.json"))


def test_missing_invalid_and_explicit_performance_rebuild_behavior(client):
    root = _root()
    node = _node(root)
    endpoint = (
        f"/nodes/{node.node_id}/category-scores/access_control/rebuild"
    )
    assert client.post(endpoint).status_code == 404
    created = client.post(f"{endpoint}?rebuild_performance=true")
    assert created.status_code == 200
    assert created.json()["record"]["finalized_submissions"] == 0
    assert client.get(
        f"/nodes/{node.node_id}/category-scores/reentrancy"
    ).status_code == 404
    assert client.post(
        f"/nodes/{node.node_id}/category-scores/not-a-category/rebuild"
    ).status_code == 400
    assert client.get(
        "/subnets/subnet_reentrancy/category-scores"
    ).status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {"weights": {"precision_weight": 1}},
        {"category_score": 1},
        {"components": {"precision": 1}},
        {"score_band": "expert"},
    ],
)
def test_api_rejects_client_controlled_score_values(client, payload):
    root = _root()
    node = _node(root)
    _store(root, node)
    response = client.post(
        f"/nodes/{node.node_id}/category-scores/access_control/rebuild",
        json=payload,
    )
    assert response.status_code == 422


def test_no_direct_editing_and_no_sensitive_future_fields(client):
    root = _root()
    node = _node(root)
    _store(root, node)
    endpoint = (
        f"/nodes/{node.node_id}/category-scores/access_control"
    )
    client.post(f"{endpoint}/rebuild")
    assert client.patch(endpoint, json={"category_score": 1}).status_code == 405
    response = client.get(endpoint)
    assert response.status_code == 200
    text = response.text.lower()
    for forbidden in (
        "/home/",
        "data/protocol",
        "private_key",
        "wallet",
        '"rank"',
        "routing_assignment",
        "stake",
    ):
        assert forbidden not in text
