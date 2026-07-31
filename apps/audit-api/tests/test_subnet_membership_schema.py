from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas.subnet import SubnetMemberRecord
from app.schemas.subnet_membership import (
    MembershipAdministrativeActionRequest,
    MembershipDecisionStatus,
    MembershipReasonCode,
    MembershipSourceSnapshot,
    SubnetMembershipDecision,
)


NOW = datetime.now(timezone.utc)


def _snapshot(**updates):
    values = {
        "node_status": "active",
        "node_supported_categories": ["access_control"],
        "subnet_status": "active",
        "subnet_minimum_category_score": 0.6,
        "subnet_minimum_finalized_submissions": 5,
        "subnet_maximum_active_nodes": 20,
        "category_score_id": "category_score_node-1_access_control",
        "category_score": 0.72,
        "score_band": "strong",
        "experience_confidence": 0.6,
        "performance_id": "performance_node-1_access_control",
        "finalized_submissions": 6,
        "accepted_unique_submissions": 3,
        "unsafe_submissions": 0,
        "recent_unsafe_event_ids": [],
        "previous_membership_status": None,
    }
    values.update(updates)
    return MembershipSourceSnapshot(**values)


def _decision(**updates):
    values = {
        "decision_id": "membership_decision_abc",
        "policy_version": "membership_policy_v0",
        "subnet_id": "subnet_access_control",
        "node_id": "node-1",
        "category": "access_control",
        "previous_status": None,
        "recommended_status": "active",
        "final_status": "active",
        "reason_codes": ["eligible_active"],
        "reasons": ["The node meets active membership requirements."],
        "source_snapshot": _snapshot(),
        "source_fingerprint": "a" * 64,
        "capacity_adjusted": False,
        "created_at": NOW,
    }
    values.update(updates)
    return SubnetMembershipDecision(**values)


def test_membership_enums_have_all_required_values():
    assert {item.value for item in MembershipDecisionStatus} == {
        "created",
        "changed",
        "unchanged",
    }
    assert {item.value for item in MembershipReasonCode} == {
        "eligible_candidate",
        "eligible_probation",
        "eligible_active",
        "eligible_expert",
        "insufficient_history",
        "insufficient_score",
        "insufficient_accepted_findings",
        "node_inactive",
        "node_suspended",
        "node_banned",
        "subnet_inactive",
        "subnet_suspended",
        "subnet_archived",
        "category_not_supported",
        "recent_unsafe_submission",
        "historical_unsafe_prevents_expert",
        "active_capacity_reached",
        "hysteresis_preserved_active",
        "hysteresis_preserved_expert",
        "administratively_suspended",
        "administratively_removed",
    }


@pytest.mark.parametrize(
    "updates",
    [
        {"category_score": -0.01},
        {"experience_confidence": 1.01},
        {"finalized_submissions": -1},
        {"recent_unsafe_event_ids": ["event-b", "event-a"]},
        {"recent_unsafe_event_ids": ["event-a", "event-a"]},
    ],
)
def test_source_snapshot_rejects_invalid_values(updates):
    with pytest.raises(ValidationError):
        _snapshot(**updates)


def test_decision_validates_reasons_fingerprint_and_category_identity():
    assert _decision().final_status.value == "active"
    for updates in (
        {"reason_codes": []},
        {"reasons": []},
        {"source_fingerprint": "A" * 64},
        {"category": "reentrancy"},
    ):
        with pytest.raises(ValidationError):
            _decision(**updates)


def test_capacity_adjustment_must_be_a_probation_downgrade():
    decision = _decision(
        final_status="probation",
        capacity_adjusted=True,
        reason_codes=["eligible_active", "active_capacity_reached"],
    )
    assert decision.capacity_adjusted
    with pytest.raises(ValidationError):
        _decision(final_status="active", capacity_adjusted=True)


def test_administrative_request_only_accepts_a_bounded_reason():
    assert MembershipAdministrativeActionRequest(
        reason="  Manual security review. "
    ).reason == "Manual security review."
    for reason in ("", "no", "x" * 501):
        with pytest.raises(ValidationError):
            MembershipAdministrativeActionRequest(reason=reason)
    with pytest.raises(ValidationError):
        MembershipAdministrativeActionRequest(
            reason="Valid reason", status="active"
        )


def test_member_schema_has_traceability_but_no_day5_or_financial_fields():
    fields = set(SubnetMemberRecord.model_fields)
    assert {
        "policy_version",
        "category_score_id",
        "category_score_source_fingerprint",
        "performance_id",
        "membership_source_fingerprint",
        "last_decision_id",
        "last_evaluated_at",
        "status_updated_at",
        "status_reason_codes",
        "administrative_lock",
    } <= fields
    assert not {
        "token",
        "stake",
        "routing_assignment",
        "reward",
    } & fields
