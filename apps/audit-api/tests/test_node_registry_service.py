import json
from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.schemas.node import NodeCreate, NodeStatusChangeRequest, NodeUpdate
from app.services.node_registry_service import (
    DuplicatePublicKeyError,
    InvalidNodeIdentifierError,
    InvalidNodeStatusTransitionError,
    NodeInactiveError,
    UnsupportedNodeCategoryError,
    change_node_status,
    create_node,
    ensure_node_can_participate,
    find_node_by_public_key,
    get_node_dir,
    is_node_active,
    list_nodes,
    load_node,
    node_supports_category,
    update_node,
)


def _payload(name="agent-one", public_key=None, **updates):
    values = {
        "node_type": "agent",
        "display_name": name,
        "operator_id": "operator-one",
        "public_key": public_key,
        "supported_categories": ["access_control"],
    }
    values.update(updates)
    return NodeCreate(**values)


def _set_status(root, node_id, status, reason=None):
    return change_node_status(
        root,
        node_id,
        NodeStatusChangeRequest(status=status, reason=reason or f"Set {status}."),
    )


def test_create_node_persists_defaults_and_utc_timestamps(tmp_path):
    node = create_node(tmp_path, _payload())
    path = tmp_path / "nodes" / node.node_id / "node.json"

    assert path.is_file()
    assert node.status.value == "active"
    assert node.reputation_score == 0.5
    assert node.statistics.model_dump() == {
        "total_submissions": 0,
        "accepted_submissions": 0,
        "rejected_submissions": 0,
        "duplicate_submissions": 0,
        "out_of_scope_submissions": 0,
        "unsafe_submissions": 0,
    }
    assert node.created_at.utcoffset() == timedelta(0)
    assert node.updated_at == node.created_at == node.status_updated_at
    assert json.loads(path.read_text(encoding="utf-8"))["node_id"] == node.node_id


def test_load_node_round_trip_and_missing_returns_none(tmp_path):
    created = create_node(tmp_path, _payload())
    assert load_node(tmp_path, created.node_id) == created
    assert load_node(tmp_path, "00000000-0000-0000-0000-000000000000") is None


def test_list_nodes_filters_and_is_deterministic(tmp_path):
    first = create_node(tmp_path, _payload("agent-one", operator_id="operator-a"))
    second = create_node(
        tmp_path,
        _payload(
            "validator-one",
            node_type="validator",
            operator_id="operator-b",
            supported_categories=["reentrancy"],
        ),
    )
    _set_status(tmp_path, second.node_id, "suspended")

    assert [node.node_id for node in list_nodes(tmp_path)] == [first.node_id, second.node_id]
    assert [node.node_id for node in list_nodes(tmp_path, node_type="agent")] == [first.node_id]
    assert [node.node_id for node in list_nodes(tmp_path, status="suspended")] == [second.node_id]
    assert [node.node_id for node in list_nodes(tmp_path, category="Access Control")] == [first.node_id]
    assert [node.node_id for node in list_nodes(tmp_path, operator_id="operator-b")] == [second.node_id]


def test_update_node_updates_only_editable_metadata(tmp_path):
    created = create_node(tmp_path, _payload())
    updated = update_node(
        tmp_path,
        created.node_id,
        NodeUpdate(display_name="renamed-agent", supported_categories=["Reentrancy"]),
    )
    assert updated.display_name == "renamed-agent"
    assert updated.supported_categories == ["reentrancy"]
    assert updated.operator_id == created.operator_id
    assert updated.node_type == created.node_type
    assert updated.reputation_score == created.reputation_score
    assert updated.statistics == created.statistics
    assert updated.created_at == created.created_at
    with pytest.raises(ValidationError):
        NodeUpdate.model_validate({"statistics": {"total_submissions": 100}})


def test_agent_update_cannot_remove_all_categories(tmp_path):
    created = create_node(tmp_path, _payload())
    with pytest.raises(UnsupportedNodeCategoryError):
        update_node(tmp_path, created.node_id, NodeUpdate(supported_categories=[]))


def test_public_key_uniqueness_and_null_reuse(tmp_path):
    first = create_node(tmp_path, _payload("agent-one", public_key="public-key-one"))
    assert find_node_by_public_key(tmp_path, "public-key-one") == first
    with pytest.raises(DuplicatePublicKeyError):
        create_node(tmp_path, _payload("agent-two", public_key="public-key-one"))

    second = create_node(tmp_path, _payload("agent-two", public_key="public-key-two"))
    with pytest.raises(DuplicatePublicKeyError):
        update_node(tmp_path, second.node_id, NodeUpdate(public_key="public-key-one"))

    create_node(tmp_path, _payload("agent-three", public_key=None))
    create_node(tmp_path, _payload("agent-four", public_key=None))


@pytest.mark.parametrize("status", ["inactive", "suspended", "banned"])
def test_only_active_nodes_are_reported_active(tmp_path, status):
    node = create_node(tmp_path, _payload())
    assert is_node_active(tmp_path, node.node_id) is True
    _set_status(tmp_path, node.node_id, status)
    assert is_node_active(tmp_path, node.node_id) is False


@pytest.mark.parametrize(
    ("initial", "target"),
    [
        ("active", "suspended"),
        ("suspended", "active"),
        ("active", "inactive"),
        ("inactive", "active"),
        ("active", "banned"),
    ],
)
def test_allowed_status_transitions(tmp_path, initial, target):
    node = create_node(tmp_path, _payload())
    if initial != "active":
        node = _set_status(tmp_path, node.node_id, initial)
    prior_status_updated_at = node.status_updated_at
    changed = _set_status(tmp_path, node.node_id, target, "Administrative reason.")
    assert changed.status.value == target
    assert changed.status_reason == "Administrative reason."
    assert changed.status_updated_at >= prior_status_updated_at
    assert load_node(tmp_path, node.node_id).status_reason == "Administrative reason."


def test_banned_node_cannot_be_reactivated(tmp_path):
    node = create_node(tmp_path, _payload())
    _set_status(tmp_path, node.node_id, "banned")
    with pytest.raises(InvalidNodeStatusTransitionError):
        _set_status(tmp_path, node.node_id, "active")


def test_category_support_has_no_validator_wildcard(tmp_path):
    agent = create_node(tmp_path, _payload())
    validator = create_node(
        tmp_path,
        _payload("validator-one", node_type="validator", supported_categories=[]),
    )
    assert node_supports_category(agent, "Access Control") is True
    assert node_supports_category(agent, "reentrancy") is False
    assert node_supports_category(validator, "access_control") is False
    with pytest.raises(UnsupportedNodeCategoryError):
        node_supports_category(agent, "not-a-real-category")


def test_ensure_node_can_participate(tmp_path):
    node = create_node(tmp_path, _payload())
    assert ensure_node_can_participate(tmp_path, node.node_id, "access-control") == node
    with pytest.raises(UnsupportedNodeCategoryError):
        ensure_node_can_participate(tmp_path, node.node_id, "reentrancy")


@pytest.mark.parametrize("status", ["inactive", "suspended", "banned"])
def test_non_active_nodes_cannot_participate(tmp_path, status):
    node = create_node(tmp_path, _payload())
    _set_status(tmp_path, node.node_id, status)
    with pytest.raises(NodeInactiveError):
        ensure_node_can_participate(tmp_path, node.node_id)


@pytest.mark.parametrize("unsafe_id", ["../escape", "a/b", "a\\b", "/absolute", ".."])
def test_unsafe_node_ids_are_rejected_and_do_not_escape_root(tmp_path, unsafe_id):
    with pytest.raises(InvalidNodeIdentifierError):
        get_node_dir(tmp_path, unsafe_id)
    assert not (tmp_path.parent / "escape").exists()
