from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.schemas.category_performance import (
    CategoryPerformanceContributionStats,
    CategoryPerformanceCounts,
    CategoryPerformanceRecord,
)


NOW = datetime.now(timezone.utc)
FINGERPRINT = "a" * 64


def _record(**updates):
    values = {
        "performance_id": "performance_node-1_access_control",
        "performance_version": "category_performance_v0",
        "node_id": "node-1",
        "category": "access_control",
        "counts": CategoryPerformanceCounts(),
        "contribution_stats": CategoryPerformanceContributionStats(),
        "source_event_ids": [],
        "source_submission_ids": [],
        "source_fingerprint": FINGERPRINT,
        "first_activity_at": None,
        "last_activity_at": None,
        "rebuilt_at": NOW,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(updates)
    return CategoryPerformanceRecord(**values)


def test_valid_counts_succeed():
    counts = CategoryPerformanceCounts(
        total_finalized_submissions=3,
        accepted_unique_submissions=1,
        duplicate_submissions=1,
        unsafe_submissions=1,
        reproduction_attempts=2,
        reproduced_submissions=1,
        reward_eligible_submissions=1,
        rewarded_submissions=1,
    )
    assert counts.total_finalized_submissions == 3


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("total_finalized_submissions", -1),
        ("accepted_unique_submissions", -1),
        ("duplicate_submissions", -1),
    ],
)
def test_negative_counts_fail(field, value):
    with pytest.raises(ValidationError):
        CategoryPerformanceCounts(**{field: value})


def test_outcome_above_total_fails():
    with pytest.raises(ValidationError):
        CategoryPerformanceCounts(
            total_finalized_submissions=1,
            accepted_unique_submissions=2,
        )


def test_multiple_primary_outcomes_cannot_double_count_one_submission():
    with pytest.raises(ValidationError):
        CategoryPerformanceCounts(
            total_finalized_submissions=1,
            rejected_submissions=1,
            unsafe_submissions=1,
        )


def test_reproduced_above_attempts_fails():
    with pytest.raises(ValidationError):
        CategoryPerformanceCounts(
            total_finalized_submissions=2,
            reproduction_attempts=1,
            reproduced_submissions=2,
        )


def test_rewarded_above_eligible_fails():
    with pytest.raises(ValidationError):
        CategoryPerformanceCounts(
            total_finalized_submissions=1,
            rewarded_submissions=1,
        )


def test_valid_contribution_stats_succeed():
    stats = CategoryPerformanceContributionStats(
        total_contribution_score=150,
        average_contribution_score=75,
        minimum_contribution_score=50,
        maximum_contribution_score=100,
        accepted_contribution_score_total=100,
        accepted_average_contribution_score=100,
    )
    assert stats.average_contribution_score == 75


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("average_contribution_score", -0.01),
        ("average_contribution_score", 100.01),
        ("accepted_average_contribution_score", -0.01),
        ("accepted_average_contribution_score", 100.01),
    ],
)
def test_invalid_average_fails(field, value):
    with pytest.raises(ValidationError):
        CategoryPerformanceContributionStats(**{field: value})


def test_minimum_above_maximum_fails():
    with pytest.raises(ValidationError):
        CategoryPerformanceContributionStats(
            minimum_contribution_score=80,
            maximum_contribution_score=20,
        )


def test_empty_performance_record_accepts_zero_statistics():
    record = _record()
    assert record.counts.total_finalized_submissions == 0
    assert record.contribution_stats.minimum_contribution_score is None


def test_performance_version_is_required_and_exact():
    values = _record().model_dump()
    values.pop("performance_version")
    with pytest.raises(ValidationError):
        CategoryPerformanceRecord(**values)
    with pytest.raises(ValidationError):
        _record(performance_version="category_performance_v1")


def test_node_identifier_is_required_and_safe():
    with pytest.raises(ValidationError):
        _record(node_id="", performance_id="performance__access_control")
    with pytest.raises(ValidationError):
        _record(node_id="../node", performance_id="performance_../node_access_control")


def test_category_must_be_supported():
    with pytest.raises(ValidationError):
        _record(category="not-a-category")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_event_ids", ["event-1", "event-1"]),
        ("source_submission_ids", ["submission-1", "submission-1"]),
        ("source_event_ids", ["event-2", "event-1"]),
        ("source_submission_ids", ["submission-2", "submission-1"]),
    ],
)
def test_source_identifiers_must_be_unique_and_sorted(field, value):
    with pytest.raises(ValidationError):
        _record(**{field: value})


def test_invalid_source_fingerprint_fails():
    with pytest.raises(ValidationError):
        _record(source_fingerprint="ABC")


def test_first_activity_after_last_activity_fails():
    counts = CategoryPerformanceCounts(
        total_finalized_submissions=1,
        accepted_unique_submissions=1,
    )
    with pytest.raises(ValidationError):
        _record(
            counts=counts,
            source_event_ids=["event-1"],
            source_submission_ids=["submission-1"],
            first_activity_at=NOW + timedelta(days=1),
            last_activity_at=NOW,
        )


def test_performance_record_has_no_future_or_sensitive_fields():
    fields = set(CategoryPerformanceRecord.model_fields)
    forbidden = {
        "category_score",
        "rank",
        "membership_status",
        "routing_assignment",
        "token",
        "stake",
        "private_key",
        "reward_pool",
    }
    assert not (fields & forbidden)
