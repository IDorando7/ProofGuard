from app.services.category_performance_service import (
    CategoryPerformanceSourceBundle,
    aggregate_category_performance,
)
from app.services.category_scoring_service import calculate_category_score
from app.services.contribution_scoring_service import (
    calculate_contribution_for_submission,
)
from app.services.node_registry_service import load_node
from app.services.reproduction_service import load_reproduction_result
from app.services.reputation_service import process_submission_reputation
from app.services.validation_service import load_validation_decision
from tests.test_finding_cluster_service import _mark_independent
from tests.test_subnet_reward_allocation_service import _setup


def test_valid_independent_duplicate_is_positive_performance_not_spam_penalty(tmp_path):
    root, workspace, _, routing, submissions = _setup(
        tmp_path, include_candidate=True
    )
    canonical = submissions[0][0]
    independent = submissions[1][0]
    validation = _mark_independent(
        workspace, independent.finding_id, canonical.finding_id
    )

    contribution = calculate_contribution_for_submission(
        root, workspace, independent.submission_id
    )
    assert contribution.total_score > 0
    assert not contribution.eligible_for_reward  # legacy reward_v0 remains unique-only
    assert not any(signal.code == "PENALTY_DUPLICATE" for signal in contribution.signals)
    assert any(
        signal.code == "INDEPENDENT_ROOT_CAUSE_DUPLICATE"
        for signal in contribution.signals
    )

    reputation = process_submission_reputation(
        root, workspace, independent.submission_id
    )
    assert reputation.reputation_delta > 0
    assert reputation.event.event_type.value == "accepted_contribution"
    assert reputation.event.event_type.value != "duplicate_finding"
    assert any(
        signal.code == "ACCEPTED_INDEPENDENT_DUPLICATE"
        for signal in reputation.event.signals
    )

    reproduction = load_reproduction_result(workspace, independent.finding_id)
    performance = aggregate_category_performance(
        load_node(root, independent.node_id),
        independent.category,
        [
            CategoryPerformanceSourceBundle(
                reputation_event=reputation.event,
                submission=independent,
                contribution=contribution,
                validation=validation,
                reproduction=reproduction,
                reward_event=None,
            )
        ],
    )
    assert performance.counts.accepted_independent_duplicate_submissions == 1
    assert performance.counts.accepted_unique_submissions == 0
    assert performance.counts.duplicate_submissions == 0
    assert performance.counts.submission_duplicate_spam == 0
    assert performance.contribution_stats.accepted_average_contribution_score > 0

    score = calculate_category_score(performance)
    assert score.penalties.duplicate_penalty == 0
    assert score.components.precision == 1
    assert score.components.experience_confidence == 0.1

    repeated = process_submission_reputation(
        root, workspace, independent.submission_id
    )
    assert repeated.processing_status.value == "already_applied"
    assert repeated.event.event_id == reputation.event.event_id
