from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas.subnet import (
    SubnetCreate,
    SubnetMemberRecord,
    SubnetMemberStatus,
    SubnetRecord,
    SubnetStatus,
    SubnetUpdate,
)


NOW = datetime.now(timezone.utc)


def _subnet_record(**updates):
    values = {
        "subnet_id": "subnet_access_control",
        "registry_version": "subnet_registry_v0",
        "category": "access_control",
        "name": "Access Control Subnet",
        "description": None,
        "status": "active",
        "minimum_category_score": 0.6,
        "minimum_finalized_submissions": 5,
        "maximum_active_nodes": 20,
        "exploration_ratio": 0.2,
        "status_reason": "Subnet registered.",
        "created_at": NOW,
        "updated_at": NOW,
        "status_updated_at": NOW,
    }
    values.update(updates)
    return SubnetRecord(**values)


def _member_record(**updates):
    values = {
        "subnet_id": "subnet_access_control",
        "node_id": "00000000-0000-0000-0000-000000000001",
        "category": "access_control",
        "status": "candidate",
        "category_score": 0.5,
        "rank": None,
        "finalized_submissions": 0,
        "accepted_unique_submissions": 0,
        "exploration_assignments": 0,
        "last_assigned_at": None,
        "joined_at": NOW,
        "updated_at": NOW,
        "status_reason": None,
        "membership_version": "subnet_membership_v0",
    }
    values.update(updates)
    return SubnetMemberRecord(**values)


def test_subnet_status_values_are_complete():
    assert {status.value for status in SubnetStatus} == {
        "active",
        "inactive",
        "suspended",
        "archived",
    }


def test_subnet_member_status_values_are_complete():
    assert {status.value for status in SubnetMemberStatus} == {
        "candidate",
        "probation",
        "active",
        "expert",
        "suspended",
        "removed",
    }


def test_subnet_create_accepts_valid_configuration_and_normalizes_category():
    payload = SubnetCreate(category=" Access-Control ")
    assert payload.category.value == "access_control"
    assert payload.minimum_category_score == 0.6


def test_unsupported_category_fails():
    with pytest.raises(ValidationError):
        SubnetCreate(category="not-a-category")


@pytest.mark.parametrize("name", ["", "  ", "ab"])
def test_empty_or_short_name_fails(name):
    with pytest.raises(ValidationError):
        SubnetCreate(category="access_control", name=name)


def test_description_over_maximum_fails():
    with pytest.raises(ValidationError):
        SubnetCreate(category="access_control", description="x" * 501)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("minimum_category_score", -0.01),
        ("minimum_category_score", 1.01),
        ("minimum_finalized_submissions", -1),
        ("maximum_active_nodes", 0),
        ("exploration_ratio", -0.01),
        ("exploration_ratio", 1.01),
    ],
)
def test_subnet_create_rejects_out_of_range_configuration(field, value):
    with pytest.raises(ValidationError):
        SubnetCreate.model_validate({"category": "access_control", field: value})


@pytest.mark.parametrize(
    ("model", "field", "value"),
    [
        (SubnetCreate, "subnet_id", "client-selected"),
        (SubnetCreate, "status", "inactive"),
        (SubnetUpdate, "category", "reentrancy"),
        (SubnetUpdate, "subnet_id", "subnet_reentrancy"),
    ],
)
def test_client_models_forbid_protected_fields(model, field, value):
    base = {"category": "access_control"} if model is SubnetCreate else {}
    with pytest.raises(ValidationError):
        model.model_validate({**base, field: value})


def test_subnet_record_requires_exact_registry_version():
    assert _subnet_record().registry_version == "subnet_registry_v0"
    with pytest.raises(ValidationError):
        _subnet_record(registry_version="subnet_registry_v1")


@pytest.mark.parametrize("score", [-0.01, 1.01])
def test_member_validates_category_score(score):
    with pytest.raises(ValidationError):
        _member_record(category_score=score)


@pytest.mark.parametrize(
    "field",
    ["finalized_submissions", "accepted_unique_submissions", "exploration_assignments"],
)
def test_member_rejects_negative_counters(field):
    with pytest.raises(ValidationError):
        _member_record(**{field: -1})


def test_member_rejects_rank_zero_and_accepts_null_rank():
    with pytest.raises(ValidationError):
        _member_record(rank=0)
    assert _member_record(rank=None).rank is None


def test_member_category_must_match_deterministic_subnet_identity():
    with pytest.raises(ValidationError):
        _member_record(category="reentrancy")


def test_subnet_models_have_no_financial_or_secret_fields():
    subnet_fields = set(SubnetRecord.model_fields)
    member_fields = set(SubnetMemberRecord.model_fields)
    forbidden = {
        "token",
        "stake",
        "private_key",
        "reward_pool",
        "wallet",
        "slashing",
    }
    assert not (subnet_fields & forbidden)
    assert not (member_fields & forbidden)
