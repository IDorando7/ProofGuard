from datetime import datetime, timezone

import pytest

from app.core.paths import protocol_data_root
from app.main import app
from app.schemas.subnet import SubnetMemberRecord
from app.services.subnet_registry_service import save_subnet_member


def _payload(category="access_control", **updates):
    values = {
        "category": category,
        "minimum_category_score": 0.6,
        "minimum_finalized_submissions": 5,
        "maximum_active_nodes": 20,
        "exploration_ratio": 0.2,
    }
    values.update(updates)
    return values


def _create(client, category="access_control", **updates):
    response = client.post("/subnets", json=_payload(category, **updates))
    assert response.status_code == 201
    return response.json()


def _root():
    return app.dependency_overrides[protocol_data_root]()


def test_post_subnets_creates_deterministic_active_record_and_duplicate_is_409(client):
    response = client.post("/subnets", json=_payload("Access Control"))
    assert response.status_code == 201
    subnet = response.json()
    assert subnet["subnet_id"] == "subnet_access_control"
    assert subnet["category"] == "access_control"
    assert subnet["status"] == "active"
    assert subnet["registry_version"] == "subnet_registry_v0"

    duplicate = client.post("/subnets", json=_payload("access-control"))
    assert duplicate.status_code == 409


def test_bootstrap_is_complete_and_idempotent(client):
    first = client.post("/subnets/bootstrap")
    second = client.post("/subnets/bootstrap")
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert len(first.json()) > 0
    assert [item["category"] for item in first.json()] == sorted(
        item["category"] for item in first.json()
    )


def test_get_subnets_lists_and_filters_records(client):
    access = _create(client)
    reentrancy = _create(client, "reentrancy")
    client.post(
        f"/subnets/{reentrancy['subnet_id']}/status",
        json={"status": "suspended", "reason": "Review."},
    )

    assert [item["subnet_id"] for item in client.get("/subnets").json()] == [
        access["subnet_id"],
        reentrancy["subnet_id"],
    ]
    assert [
        item["subnet_id"]
        for item in client.get("/subnets?category=Access%20Control").json()
    ] == [access["subnet_id"]]
    assert [
        item["subnet_id"]
        for item in client.get("/subnets?status=suspended").json()
    ] == [reentrancy["subnet_id"]]


def test_read_subnet_and_read_by_category(client):
    subnet = _create(client)
    assert client.get(f"/subnets/{subnet['subnet_id']}").json() == subnet
    assert client.get("/subnets/by-category/Access%20Control").json() == subnet
    assert client.get("/subnets/subnet_reentrancy").status_code == 404
    assert client.get("/subnets/by-category/not-a-category").status_code == 400


def test_patch_updates_only_allowed_fields(client):
    subnet = _create(client)
    response = client.patch(
        f"/subnets/{subnet['subnet_id']}",
        json={
            "name": "Authorization Specialists",
            "minimum_category_score": 0.8,
            "maximum_active_nodes": 35,
        },
    )
    assert response.status_code == 200
    updated = response.json()
    assert updated["name"] == "Authorization Specialists"
    assert updated["minimum_category_score"] == 0.8
    assert updated["maximum_active_nodes"] == 35
    assert updated["subnet_id"] == subnet["subnet_id"]
    assert updated["category"] == subnet["category"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("subnet_id", "subnet_reentrancy"),
        ("category", "reentrancy"),
        ("status", "inactive"),
        ("registry_version", "subnet_registry_v1"),
        ("created_at", "2026-07-27T10:00:00Z"),
    ],
)
def test_patch_rejects_protected_fields(client, field, value):
    subnet = _create(client)
    response = client.patch(f"/subnets/{subnet['subnet_id']}", json={field: value})
    assert response.status_code == 422


def test_status_endpoint_changes_status_and_archived_is_terminal(client):
    subnet = _create(client)
    suspended = client.post(
        f"/subnets/{subnet['subnet_id']}/status",
        json={"status": "suspended", "reason": "Administrative review."},
    )
    assert suspended.status_code == 200
    assert suspended.json()["status"] == "suspended"
    assert suspended.json()["status_reason"] == "Administrative review."

    archived = client.post(
        f"/subnets/{subnet['subnet_id']}/status",
        json={"status": "archived", "reason": "Retired."},
    )
    assert archived.status_code == 200
    reactivation = client.post(
        f"/subnets/{subnet['subnet_id']}/status",
        json={"status": "active", "reason": "Attempted reactivation."},
    )
    assert reactivation.status_code == 409


def test_member_list_is_empty_and_missing_member_is_404(client):
    subnet = _create(client)
    response = client.get(f"/subnets/{subnet['subnet_id']}/members")
    assert response.status_code == 200
    assert response.json() == {
        "subnet_id": subnet["subnet_id"],
        "category": "access_control",
        "total": 0,
        "members": [],
    }
    missing = client.get(
        f"/subnets/{subnet['subnet_id']}/members/"
        "00000000-0000-0000-0000-000000000000"
    )
    assert missing.status_code == 404


def test_member_read_and_status_filter_use_internal_records_only(client):
    subnet = _create(client)
    node_response = client.post(
        "/nodes",
        json={
            "node_type": "agent",
            "display_name": "access-member",
            "operator_id": "operator-member",
            "supported_categories": ["access_control"],
        },
    )
    assert node_response.status_code == 201
    node = node_response.json()
    now = datetime.now(timezone.utc)
    member = save_subnet_member(
        _root(),
        SubnetMemberRecord(
            subnet_id=subnet["subnet_id"],
            node_id=node["node_id"],
            category="access_control",
            status="probation",
            category_score=0.42,
            rank=None,
            finalized_submissions=2,
            accepted_unique_submissions=1,
            exploration_assignments=3,
            last_assigned_at=None,
            joined_at=now,
            updated_at=now,
            status_reason="Controlled internal fixture.",
            membership_version="subnet_membership_v0",
        ),
    )

    listed = client.get(f"/subnets/{subnet['subnet_id']}/members?status=probation")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["members"][0]["node_id"] == node["node_id"]
    assert client.get(
        f"/subnets/{subnet['subnet_id']}/members?status=expert"
    ).json()["members"] == []
    read = client.get(f"/subnets/{subnet['subnet_id']}/members/{node['node_id']}")
    assert read.status_code == 200
    assert read.json()["category_score"] == member.category_score


def test_no_public_member_creation_route_exists(client):
    subnet = _create(client)
    response = client.post(
        f"/subnets/{subnet['subnet_id']}/members",
        json={"node_id": "client-controlled", "status": "expert", "category_score": 1},
    )
    assert response.status_code == 405


def test_api_responses_expose_no_paths_secrets_tokens_or_staking_fields(client):
    response = client.post("/subnets", json=_payload())
    assert response.status_code == 201
    text = response.text
    body = response.json()
    assert "/home/" not in text
    assert "data/protocol" not in text
    for field in ("private_key", "token", "stake", "reward_pool", "wallet"):
        assert field not in body

    for forbidden in (
        {"private_key": "secret"},
        {"token": "PG"},
        {"stake": 100},
        {"reward_pool": 1000},
    ):
        rejected = client.post("/subnets", json={**_payload(), **forbidden})
        assert rejected.status_code == 422
