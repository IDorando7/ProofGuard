from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas.node import NodeCreate, NodeRecord, NodeStatistics, NodeUpdate


def _create_payload(**updates):
    payload = {
        "node_type": "agent",
        "display_name": "access-control-agent-01",
        "operator_id": "operator-01",
        "supported_categories": ["access_control"],
    }
    payload.update(updates)
    return payload


def _record_payload(**updates):
    now = datetime.now(timezone.utc)
    payload = {
        **_create_payload(),
        "node_id": "node-01",
        "status": "active",
        "reputation_score": 0.5,
        "statistics": {},
        "status_reason": "Node registered.",
        "created_at": now,
        "updated_at": now,
        "status_updated_at": now,
    }
    payload.update(updates)
    return payload


@pytest.mark.parametrize("node_type", ["agent", "hybrid"])
def test_agent_and_hybrid_require_and_accept_category(node_type):
    node = NodeCreate(**_create_payload(node_type=node_type))
    assert node.supported_categories == ["access_control"]


def test_validator_accepts_empty_categories():
    node = NodeCreate(**_create_payload(node_type="validator", supported_categories=[]))
    assert node.supported_categories == []


@pytest.mark.parametrize("node_type", ["agent", "hybrid"])
def test_agent_and_hybrid_reject_empty_categories(node_type):
    with pytest.raises(ValidationError, match="at least one supported category"):
        NodeCreate(**_create_payload(node_type=node_type, supported_categories=[]))


@pytest.mark.parametrize("category", ["Access Control", "access-control", " access_control "])
def test_category_normalization(category):
    node = NodeCreate(**_create_payload(supported_categories=[category]))
    assert node.supported_categories == ["access_control"]


def test_duplicate_categories_are_removed_in_input_order():
    node = NodeCreate(
        **_create_payload(supported_categories=["Reentrancy", "Access Control", "reentrancy"])
    )
    assert node.supported_categories == ["reentrancy", "access_control"]


def test_unsupported_category_fails():
    with pytest.raises(ValidationError, match="Unsupported vulnerability category"):
        NodeCreate(**_create_payload(supported_categories=["invented-category"]))


@pytest.mark.parametrize("display_name", ["", "  ", "ab"])
def test_empty_or_short_display_name_fails(display_name):
    with pytest.raises(ValidationError):
        NodeCreate(**_create_payload(display_name=display_name))


def test_empty_operator_id_fails():
    with pytest.raises(ValidationError):
        NodeCreate(**_create_payload(operator_id="  "))


@pytest.mark.parametrize("score", [-0.01, 1.01])
def test_record_reputation_must_be_bounded(score):
    with pytest.raises(ValidationError):
        NodeRecord(**_record_payload(reputation_score=score))


def test_negative_statistics_fail():
    with pytest.raises(ValidationError):
        NodeStatistics(total_submissions=-1)


@pytest.mark.parametrize("field", ["reputation_score", "statistics", "node_type", "operator_id", "status"])
def test_node_update_rejects_protected_fields(field):
    with pytest.raises(ValidationError):
        NodeUpdate.model_validate({field: 0.9 if field == "reputation_score" else {}})


def test_request_schemas_never_define_private_key_or_secret_fields():
    forbidden = {"private_key", "mnemonic", "seed_phrase", "api_key", "authentication_token"}
    assert forbidden.isdisjoint(NodeCreate.model_fields)
    assert forbidden.isdisjoint(NodeUpdate.model_fields)
    with pytest.raises(ValidationError):
        NodeCreate(**_create_payload(private_key="must-not-be-accepted"))
