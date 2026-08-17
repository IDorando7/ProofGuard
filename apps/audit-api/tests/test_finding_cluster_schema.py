from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas.finding_cluster import (
    FindingCluster,
    FindingClusterMember,
)


NOW = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)


def _member(submission_id="submission-1", operator_id="operator-1", **updates):
    values = {
        "submission_id": submission_id,
        "finding_id": f"finding-{submission_id}",
        "node_id": f"node-{submission_id}",
        "operator_id": operator_id,
        "validation_id": f"validation-{submission_id}",
        "reproduction_id": f"reproduction-{submission_id}",
        "submitted_at": NOW,
        "relation": "canonical",
        "source_fingerprint": "a" * 64,
    }
    values.update(updates)
    return FindingClusterMember(**values)


def _cluster(members=None, **updates):
    members = members or [_member()]
    canonical = next(
        (
            member
            for member in members
            if getattr(member.relation, "value", member.relation) == "canonical"
        ),
        members[0],
    )
    values = {
        "finding_cluster_id": f"finding_cluster_{'b' * 64}",
        "project_id": "project-1",
        "routing_id": "routing-1",
        "category": "reentrancy",
        "canonical_finding_id": canonical.finding_id,
        "canonical_submission_id": canonical.submission_id,
        "final_severity": "High",
        "root_cause_key": "reentrancy:src/Vault.sol:withdraw",
        "root_cause_fingerprint": "c" * 64,
        "members": members,
        "report_count": len(members),
        "distinct_operator_count": len({member.operator_id for member in members}),
        "status": "open",
        "source_fingerprint": "d" * 64,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(updates)
    return FindingCluster(**values)


def test_valid_cluster_and_duplicate_operator_count():
    canonical = _member()
    duplicate = _member(
        "submission-2",
        operator_id="operator-1",
        submitted_at=NOW,
        relation="independent_duplicate",
    )
    cluster = _cluster([canonical, duplicate], distinct_operator_count=1)
    assert cluster.report_count == 2
    assert cluster.distinct_operator_count == 1


@pytest.mark.parametrize(
    "change",
    [
        {"project_id": ""},
        {"routing_id": ""},
        {"category": "not-a-category"},
        {"canonical_finding_id": "missing"},
        {"report_count": 2},
        {"distinct_operator_count": 2},
        {"root_cause_fingerprint": "bad"},
        {"final_severity": "EXTREME"},
    ],
)
def test_cluster_rejects_invalid_identity_counts_fingerprint_and_severity(change):
    with pytest.raises(ValidationError):
        _cluster(**change)


def test_member_ids_and_order_are_deterministic_and_canonical_must_exist():
    first = _member("submission-1")
    duplicate_id = first.model_copy(update={"relation": "independent_duplicate"})
    with pytest.raises(ValidationError):
        _cluster([first, duplicate_id])

    later_canonical = _member("submission-2", submitted_at=NOW)
    earlier_duplicate = _member(
        "submission-1", relation="independent_duplicate", submitted_at=NOW
    )
    with pytest.raises(ValidationError):
        _cluster([later_canonical, earlier_duplicate])
    with pytest.raises(ValidationError):
        _cluster(
            [first.model_copy(update={"relation": "independent_duplicate"})]
        )


def test_cluster_schema_has_no_reward_wallet_or_private_key_fields():
    cluster_fields = set(FindingCluster.model_fields)
    member_fields = set(FindingClusterMember.model_fields)
    forbidden = {
        "reward",
        "reward_amount",
        "finding_score",
        "uniqueness",
        "quality_rank",
        "chief_finder",
        "wallet",
        "wallet_address",
        "private_key",
        "mnemonic",
    }
    assert not cluster_fields.intersection(forbidden)
    assert not member_fields.intersection(forbidden)
