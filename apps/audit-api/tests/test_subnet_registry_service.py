import json
from datetime import datetime, timedelta, timezone

import pytest

from app.schemas.finding import FindingCategory
from app.schemas.node import NodeCreate
from app.schemas.subnet import (
    SubnetCreate,
    SubnetMemberRecord,
    SubnetMemberStatus,
    SubnetStatusChangeRequest,
    SubnetUpdate,
)
from app.services.node_registry_service import create_node
from app.services.subnet_registry_service import (
    InvalidSubnetIdentifierError,
    InvalidSubnetStatusTransitionError,
    SubnetArchivedError,
    SubnetCategoryAlreadyRegisteredError,
    SubnetInactiveError,
    SubnetMemberCategoryMismatchError,
    SubnetNodeNotFoundError,
    SubnetNotFoundError,
    UnsupportedSubnetCategoryError,
    bootstrap_default_subnets,
    build_subnet_id,
    change_subnet_status,
    create_subnet,
    ensure_subnet_active,
    find_subnet_by_category,
    get_subnet_dir,
    is_subnet_active,
    list_subnet_members,
    list_subnets,
    load_subnet,
    load_subnet_member,
    save_subnet_member,
    update_subnet,
)


def _create_payload(category="access_control", **updates):
    values = {"category": category}
    values.update(updates)
    return SubnetCreate(**values)


def _set_status(root, subnet_id, status, reason=None):
    return change_subnet_status(
        root,
        subnet_id,
        SubnetStatusChangeRequest(status=status, reason=reason or f"Set {status}."),
    )


def _create_node(root, name="member-node"):
    return create_node(
        root,
        NodeCreate(
            node_type="agent",
            display_name=name,
            operator_id=f"operator-{name}",
            supported_categories=["access_control"],
        ),
    )


def _member(subnet_id, node_id, **updates):
    now = datetime.now(timezone.utc)
    values = {
        "subnet_id": subnet_id,
        "node_id": node_id,
        "category": "access_control",
        "status": "candidate",
        "category_score": 0.5,
        "rank": None,
        "finalized_submissions": 0,
        "accepted_unique_submissions": 0,
        "exploration_assignments": 0,
        "last_assigned_at": None,
        "joined_at": now,
        "updated_at": now,
        "status_reason": "Stored by a controlled internal service.",
        "membership_version": "subnet_membership_v0",
    }
    values.update(updates)
    return SubnetMemberRecord(**values)


def test_build_subnet_id_is_normalized_and_deterministic():
    assert build_subnet_id("access_control") == "subnet_access_control"
    assert build_subnet_id("Access Control") == "subnet_access_control"
    assert build_subnet_id("access-control") == "subnet_access_control"
    assert build_subnet_id(" Oracle Manipulation ") == "subnet_oracle_manipulation"
    assert build_subnet_id("access_control") == build_subnet_id("Access Control")


@pytest.mark.parametrize("category", ["../access_control", "access/control", r"access\\control", "unknown"])
def test_unsafe_or_unsupported_categories_are_rejected(category):
    with pytest.raises(UnsupportedSubnetCategoryError):
        build_subnet_id(category)


def test_create_load_find_and_duplicate_behavior(tmp_path):
    created = create_subnet(tmp_path, _create_payload())
    path = tmp_path / "subnets" / "subnet_access_control" / "subnet.json"

    assert path.is_file()
    assert created.status.value == "active"
    assert created.registry_version == "subnet_registry_v0"
    assert created.name == "Access Control Subnet"
    assert load_subnet(tmp_path, created.subnet_id) == created
    assert find_subnet_by_category(tmp_path, "Access-Control") == created
    assert load_subnet(tmp_path, "subnet_reentrancy") is None
    with pytest.raises(SubnetCategoryAlreadyRegisteredError):
        create_subnet(tmp_path, _create_payload(name="Different Name"))


def test_explicit_name_and_human_readable_safe_json_are_preserved(tmp_path):
    created = create_subnet(
        tmp_path,
        _create_payload(name="Authorization Specialists", description="Off-chain registry metadata."),
    )
    path = tmp_path / "subnets" / created.subnet_id / "subnet.json"
    text = path.read_text(encoding="utf-8")
    stored = json.loads(text)

    assert created.name == "Authorization Specialists"
    assert text.startswith("{\n  ")
    assert text.endswith("\n")
    assert "/home/" not in text
    assert str(tmp_path) not in text
    assert stored["subnet_id"] == "subnet_access_control"


def test_list_subnets_filters_and_sorts_by_category(tmp_path):
    reentrancy = create_subnet(tmp_path, _create_payload("reentrancy"))
    access = create_subnet(tmp_path, _create_payload("access_control"))
    _set_status(tmp_path, reentrancy.subnet_id, "suspended")

    assert [item.subnet_id for item in list_subnets(tmp_path)] == [
        access.subnet_id,
        reentrancy.subnet_id,
    ]
    assert [item.subnet_id for item in list_subnets(tmp_path, category="Access Control")] == [
        access.subnet_id
    ]
    assert [item.subnet_id for item in list_subnets(tmp_path, status="suspended")] == [
        reentrancy.subnet_id
    ]


def test_update_changes_only_editable_configuration(tmp_path):
    created = create_subnet(tmp_path, _create_payload())
    updated = update_subnet(
        tmp_path,
        created.subnet_id,
        SubnetUpdate(
            name="Access Specialists",
            description="Updated.",
            minimum_category_score=0.8,
            minimum_finalized_submissions=12,
            maximum_active_nodes=42,
            exploration_ratio=0.1,
        ),
    )
    assert updated.name == "Access Specialists"
    assert updated.description == "Updated."
    assert updated.minimum_category_score == 0.8
    assert updated.minimum_finalized_submissions == 12
    assert updated.maximum_active_nodes == 42
    assert updated.exploration_ratio == 0.1
    assert updated.category == created.category
    assert updated.subnet_id == created.subnet_id
    assert updated.registry_version == created.registry_version
    assert updated.created_at == created.created_at
    assert updated.updated_at >= created.updated_at


@pytest.mark.parametrize(
    ("initial", "target"),
    [
        ("active", "inactive"),
        ("inactive", "active"),
        ("active", "suspended"),
        ("suspended", "active"),
        ("inactive", "suspended"),
        ("suspended", "inactive"),
        ("active", "archived"),
        ("inactive", "archived"),
        ("suspended", "archived"),
    ],
)
def test_allowed_status_transitions(tmp_path, initial, target):
    subnet = create_subnet(tmp_path, _create_payload())
    if initial != "active":
        subnet = _set_status(tmp_path, subnet.subnet_id, initial)
    previous_status_time = subnet.status_updated_at
    changed = _set_status(tmp_path, subnet.subnet_id, target, "Administrative review.")

    assert changed.status.value == target
    assert changed.status_reason == "Administrative review."
    assert changed.status_updated_at > previous_status_time
    assert load_subnet(tmp_path, subnet.subnet_id) == changed


@pytest.mark.parametrize("target", ["active", "inactive", "suspended"])
def test_archived_status_is_terminal(tmp_path, target):
    subnet = create_subnet(tmp_path, _create_payload())
    _set_status(tmp_path, subnet.subnet_id, "archived")
    with pytest.raises(InvalidSubnetStatusTransitionError):
        _set_status(tmp_path, subnet.subnet_id, target)


def test_same_status_change_is_idempotent(tmp_path):
    subnet = create_subnet(tmp_path, _create_payload())
    same = _set_status(tmp_path, subnet.subnet_id, "active", "Different reason.")
    assert same == subnet


def test_archived_configuration_cannot_be_updated(tmp_path):
    subnet = create_subnet(tmp_path, _create_payload())
    _set_status(tmp_path, subnet.subnet_id, "archived")
    with pytest.raises(SubnetArchivedError):
        update_subnet(tmp_path, subnet.subnet_id, SubnetUpdate(name="Archived Rename"))


@pytest.mark.parametrize(
    ("status", "expected_error"),
    [
        ("inactive", SubnetInactiveError),
        ("suspended", SubnetInactiveError),
        ("archived", SubnetArchivedError),
    ],
)
def test_active_helpers_reject_disabled_subnets(tmp_path, status, expected_error):
    subnet = create_subnet(tmp_path, _create_payload())
    assert is_subnet_active(tmp_path, subnet.subnet_id) is True
    assert ensure_subnet_active(tmp_path, subnet.subnet_id) == subnet

    _set_status(tmp_path, subnet.subnet_id, status)
    assert is_subnet_active(tmp_path, subnet.subnet_id) is False
    with pytest.raises(expected_error):
        ensure_subnet_active(tmp_path, subnet.subnet_id)


def test_ensure_active_rejects_missing_subnet(tmp_path):
    with pytest.raises(SubnetNotFoundError):
        ensure_subnet_active(tmp_path, "subnet_access_control")


@pytest.mark.parametrize(
    "unsafe_id",
    ["../subnet_access_control", "subnet/access_control", r"subnet\\access_control", "/absolute", ".."],
)
def test_subnet_path_traversal_is_rejected(tmp_path, unsafe_id):
    with pytest.raises(InvalidSubnetIdentifierError):
        get_subnet_dir(tmp_path, unsafe_id)
    assert not (tmp_path.parent / "subnet_access_control").exists()


def test_bootstrap_is_complete_idempotent_and_preserves_configuration(tmp_path):
    first = bootstrap_default_subnets(tmp_path)
    expected_categories = sorted(category.value for category in FindingCategory)

    assert [subnet.category.value for subnet in first] == expected_categories
    assert [subnet.subnet_id for subnet in first] == [
        f"subnet_{category}" for category in expected_categories
    ]

    access = find_subnet_by_category(tmp_path, "access_control")
    customized = update_subnet(
        tmp_path,
        access.subnet_id,
        SubnetUpdate(name="Custom Access Registry", minimum_category_score=0.91),
    )
    _set_status(tmp_path, customized.subnet_id, "inactive")
    second = bootstrap_default_subnets(tmp_path)
    preserved = find_subnet_by_category(tmp_path, "access_control")

    assert len(second) == len(FindingCategory)
    assert preserved.name == "Custom Access Registry"
    assert preserved.minimum_category_score == 0.91
    assert preserved.status.value == "inactive"
    assert not list((tmp_path / "subnets").glob("*/members/*.json"))
    assert not (tmp_path / "rewards").exists()
    assert not (tmp_path / "routing").exists()


@pytest.mark.parametrize("disabled_status", ["suspended", "archived"])
def test_bootstrap_does_not_reactivate_suspended_or_archived_subnets(tmp_path, disabled_status):
    bootstrap_default_subnets(tmp_path)
    access = find_subnet_by_category(tmp_path, "access_control")
    _set_status(tmp_path, access.subnet_id, disabled_status)
    bootstrap_default_subnets(tmp_path)
    assert find_subnet_by_category(tmp_path, "access_control").status.value == disabled_status


def test_member_save_requires_existing_subnet_and_node(tmp_path):
    unknown_member = _member(
        "subnet_access_control",
        "00000000-0000-0000-0000-000000000001",
    )
    with pytest.raises(SubnetNotFoundError):
        save_subnet_member(tmp_path, unknown_member)

    subnet = create_subnet(tmp_path, _create_payload())
    with pytest.raises(SubnetNodeNotFoundError):
        save_subnet_member(tmp_path, unknown_member)
    assert list_subnet_members(tmp_path, subnet.subnet_id) == []


def test_member_category_must_match_subnet(tmp_path):
    subnet = create_subnet(tmp_path, _create_payload())
    node = _create_node(tmp_path)
    mismatched = _member(subnet.subnet_id, node.node_id).model_copy(
        update={"category": FindingCategory.REENTRANCY}
    )
    with pytest.raises(SubnetMemberCategoryMismatchError):
        save_subnet_member(tmp_path, mismatched)


def test_member_round_trip_filtering_sorting_and_update_preserves_joined_at(tmp_path):
    subnet = create_subnet(tmp_path, _create_payload())
    nodes = [_create_node(tmp_path, f"member-{index}") for index in range(4)]
    members = [
        _member(
            subnet.subnet_id,
            nodes[0].node_id,
            status="active",
            rank=2,
            category_score=0.8,
        ),
        _member(
            subnet.subnet_id,
            nodes[1].node_id,
            status="expert",
            rank=1,
            category_score=0.7,
        ),
        _member(
            subnet.subnet_id,
            nodes[2].node_id,
            status="active",
            rank=2,
            category_score=0.9,
        ),
        _member(
            subnet.subnet_id,
            nodes[3].node_id,
            status="active",
            rank=None,
            category_score=0.99,
        ),
    ]
    stored = [save_subnet_member(tmp_path, member) for member in members]

    assert (
        tmp_path
        / "subnets"
        / subnet.subnet_id
        / "members"
        / f"{nodes[0].node_id}.json"
    ).is_file()
    assert load_subnet_member(tmp_path, subnet.subnet_id, nodes[0].node_id) == stored[0]
    assert load_subnet_member(
        tmp_path,
        subnet.subnet_id,
        "00000000-0000-0000-0000-000000000099",
    ) is None
    assert [member.node_id for member in list_subnet_members(tmp_path, subnet.subnet_id)] == [
        nodes[1].node_id,
        nodes[2].node_id,
        nodes[0].node_id,
        nodes[3].node_id,
    ]
    active_members = list_subnet_members(tmp_path, subnet.subnet_id, status="active")
    assert {member.node_id for member in active_members} == {
        nodes[0].node_id,
        nodes[2].node_id,
        nodes[3].node_id,
    }

    original = stored[0]
    attempted_joined_at = original.joined_at + timedelta(days=1)
    updated_input = original.model_copy(
        update={
            "status": SubnetMemberStatus.EXPERT,
            "category_score": 0.812345,
            "rank": 1,
            "joined_at": attempted_joined_at,
        }
    )
    updated = save_subnet_member(tmp_path, updated_input)
    assert updated.joined_at == original.joined_at
    assert updated.category_score == 0.812345
    assert updated.status.value == "expert"
    assert updated.rank == 1
