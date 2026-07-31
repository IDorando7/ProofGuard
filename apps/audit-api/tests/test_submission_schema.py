from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas.submission import (
    SubmissionCreate,
    SubmissionRecord,
    SubmissionRewardStatus,
    SubmissionStatus,
)


def _create_payload(**updates):
    payload = {"project_id": "project-1", "finding_id": "finding-1", "node_id": "node-1"}
    payload.update(updates)
    return payload


def _record_payload(**updates):
    now = datetime.now(timezone.utc)
    payload = {
        "submission_id": "submission-1",
        "project_id": "project-1",
        "finding_id": "finding-1",
        "node_id": "node-1",
        "node_type": "agent",
        "agent_name": "access_control_agent",
        "agent_version": "0.1.0",
        "category": "access_control",
        "finding_hash": "a" * 64,
        "status": "submitted",
        "reproduction_id": None,
        "validation_id": None,
        "reward_status": "pending",
        "metadata": {},
        "submitted_at": now,
        "created_at": now,
        "updated_at": now,
        "status_updated_at": now,
        "status_reason": "Finding submission created.",
    }
    payload.update(updates)
    return payload


def test_submission_create_accepts_valid_required_fields():
    submission = SubmissionCreate(**_create_payload())
    assert submission.project_id == "project-1"
    assert submission.metadata == {}


@pytest.mark.parametrize("field", ["project_id", "finding_id", "node_id"])
@pytest.mark.parametrize("value", ["", "   "])
def test_submission_create_rejects_empty_required_ids(field, value):
    with pytest.raises(ValidationError):
        SubmissionCreate(**_create_payload(**{field: value}))


def test_agent_name_and_version_lengths_are_validated():
    with pytest.raises(ValidationError):
        SubmissionCreate(**_create_payload(agent_name="a" * 129))
    with pytest.raises(ValidationError):
        SubmissionCreate(**_create_payload(agent_version="v" * 65))


def test_metadata_default_is_independent():
    first = SubmissionCreate(**_create_payload())
    second = SubmissionCreate(**_create_payload(finding_id="finding-2"))
    first.metadata["runtime"] = "local"
    assert second.metadata == {}


def test_metadata_must_be_small_json_without_sensitive_fields():
    with pytest.raises(ValidationError, match="JSON serializable"):
        SubmissionCreate(**_create_payload(metadata={"runtime": object()}))
    with pytest.raises(ValidationError, match="not exceed"):
        SubmissionCreate(**_create_payload(metadata={"runtime": "x" * 5000}))
    with pytest.raises(ValidationError, match="not allowed"):
        SubmissionCreate(**_create_payload(metadata={"poc_content": "exploit"}))
    with pytest.raises(ValidationError, match="host paths"):
        SubmissionCreate(**_create_payload(metadata={"workspace": "/home/user/audit"}))


def test_submission_record_accepts_lowercase_sha256_hash():
    record = SubmissionRecord(**_record_payload())
    assert record.finding_hash == "a" * 64


@pytest.mark.parametrize("finding_hash", ["a" * 63, "g" * 64, "A" * 64])
def test_submission_record_rejects_invalid_hash(finding_hash):
    with pytest.raises(ValidationError):
        SubmissionRecord(**_record_payload(finding_hash=finding_hash))


def test_submission_record_requires_timezone_aware_timestamps():
    with pytest.raises(ValidationError, match="timezone-aware"):
        SubmissionRecord(**_record_payload(created_at=datetime.now()))


@pytest.mark.parametrize(
    "field",
    [
        "submission_id",
        "finding_hash",
        "category",
        "status",
        "validation_id",
        "reproduction_id",
        "reward_status",
        "created_at",
        "reputation_score",
        "reward_amount",
        "private_key",
    ],
)
def test_submission_create_rejects_service_controlled_and_secret_fields(field):
    with pytest.raises(ValidationError):
        SubmissionCreate.model_validate({**_create_payload(), field: "client-value"})


def test_request_schema_has_no_private_key_fields():
    forbidden = {"private_key", "mnemonic", "seed_phrase", "api_key", "secret"}
    assert forbidden.isdisjoint(SubmissionCreate.model_fields)


def test_submission_status_has_all_required_values():
    assert {status.value for status in SubmissionStatus} == {
        "submitted",
        "reproduction_pending",
        "validation_pending",
        "accepted",
        "rejected",
        "duplicate",
        "out_of_scope",
        "insufficient_evidence",
        "needs_review",
        "unsafe",
        "unsupported",
        "reward_pending",
        "rewarded",
        "penalized",
    }
    assert {status.value for status in SubmissionRewardStatus} == {
        "pending",
        "eligible",
        "ineligible",
        "rewarded",
        "penalized",
    }
