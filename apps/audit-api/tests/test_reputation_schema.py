from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas.node import NodeStatistics
from app.schemas.reputation import (
    NodeStatisticsDelta,
    ReputationDeltaComponents,
    ReputationEvent,
    ReputationEventApplicationStatus,
    ReputationEventType,
    ReputationProcessRequest,
    ReputationProcessingStatus,
    ReputationSignal,
)


def _event(**updates):
    now = datetime.now(timezone.utc)
    values = {
        "event_id": "event-1",
        "reputation_version": "reputation_v0",
        "application_status": "applied",
        "node_id": "node-1",
        "submission_id": "submission-1",
        "project_id": "project-1",
        "finding_id": "finding-1",
        "category": "access_control",
        "validation_id": "validation-1",
        "reproduction_id": "reproduction-1",
        "contribution_score_id": "score-1",
        "validation_status": "accepted",
        "reproduction_status": "reproduced",
        "contribution_score": 92,
        "contribution_eligibility_status": "eligible",
        "normalized_severity": "High",
        "event_type": "accepted_contribution",
        "previous_reputation": 0.5,
        "delta_components": ReputationDeltaComponents(
            base_delta=0.03,
            contribution_bonus=0.02,
            severity_bonus=0.01,
            total_delta=0.06,
        ),
        "new_reputation": 0.56,
        "statistics_delta": NodeStatisticsDelta(total_submissions=1, accepted_submissions=1),
        "previous_statistics": NodeStatistics().model_dump(),
        "new_statistics": NodeStatistics(total_submissions=1, accepted_submissions=1).model_dump(),
        "signals": [],
        "reason": "Deterministic reputation change.",
        "source_fingerprint": "a" * 64,
        "created_at": now,
        "applied_at": now,
        "updated_at": now,
    }
    values.update(updates)
    return ReputationEvent(**values)


def test_reputation_enums_have_exact_values():
    assert {item.value for item in ReputationEventType} == {
        "accepted_contribution",
        "duplicate_finding",
        "rejected_finding",
        "out_of_scope_finding",
        "insufficient_evidence",
        "unsafe_submission",
        "unsupported_submission",
    }
    assert {item.value for item in ReputationEventApplicationStatus} == {"prepared", "applied"}
    assert {item.value for item in ReputationProcessingStatus} == {"applied", "already_applied", "pending"}


def test_delta_components_validate_ranges_and_sum():
    valid = ReputationDeltaComponents(
        base_delta=0.03,
        contribution_bonus=0.02,
        severity_bonus=0.01,
        total_delta=0.06,
    )
    assert valid.total_delta == 0.06
    for values in (
        {"penalty": 0.01, "total_delta": 0.01},
        {"contribution_bonus": -0.01, "total_delta": -0.01},
        {"severity_bonus": -0.01, "total_delta": -0.01},
        {"base_delta": 1, "contribution_bonus": 1, "total_delta": 2},
        {"base_delta": 0.03, "total_delta": 0.02},
    ):
        with pytest.raises(ValidationError):
            ReputationDeltaComponents(**values)


def test_signal_validates_text_and_range():
    assert ReputationSignal(code="ACCEPTED_BASE", delta=0.03, message="Accepted.").delta == 0.03
    for values in (
        {"code": "", "delta": 0, "message": "message"},
        {"code": "code", "delta": 0, "message": ""},
        {"code": "code", "delta": 1.01, "message": "message"},
    ):
        with pytest.raises(ValidationError):
            ReputationSignal(**values)


@pytest.mark.parametrize("value", [-1, 2])
def test_statistics_delta_accepts_only_zero_or_one(value):
    with pytest.raises(ValidationError):
        NodeStatisticsDelta(total_submissions=value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("previous_reputation", -0.01),
        ("new_reputation", 1.01),
        ("contribution_score", -1),
        ("contribution_score", 101),
        ("source_fingerprint", "short"),
        ("source_fingerprint", "g" * 64),
        ("category", "not-a-category"),
    ],
)
def test_event_validates_ranges_fingerprint_and_category(field, value):
    with pytest.raises(ValidationError):
        _event(**{field: value})


def test_applied_requires_applied_at_but_prepared_may_omit_it():
    with pytest.raises(ValidationError):
        _event(applied_at=None)
    prepared = _event(application_status="prepared", applied_at=None)
    assert prepared.applied_at is None


def test_reputation_schemas_contain_no_reward_token_or_private_fields():
    fields = set(ReputationEvent.model_fields)
    assert not fields.intersection(
        {"reward_amount", "token_amount", "private_key", "mnemonic", "stake", "slash_amount"}
    )


@pytest.mark.parametrize(
    "field",
    ["reputation_delta", "current_reputation", "statistics", "event_type", "application_status", "private_key"],
)
def test_public_process_request_rejects_client_controlled_fields(field):
    with pytest.raises(ValidationError):
        ReputationProcessRequest.model_validate({field: 1})
