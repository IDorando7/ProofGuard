import sys
import tempfile
from pathlib import Path

import pytest


APP_ROOT = Path(__file__).resolve().parents[1]
EVAL_ROOT = APP_ROOT / "research" / "evals"
if str(EVAL_ROOT) not in sys.path:
    sys.path.insert(0, str(EVAL_ROOT))

import eval_protocol  # noqa: E402


@pytest.fixture(scope="module")
def benchmark():
    with tempfile.TemporaryDirectory(prefix="proofguard-protocol-integration-") as directory:
        yield eval_protocol.run_protocol_benchmark(results_dir=Path(directory))


def _case(case_document, case_id):
    return next(result for result in case_document["cases"] if result["case_id"] == case_id)


def test_accepted_flow_registers_submits_scores_reputes_and_rewards(benchmark):
    cases, _ = benchmark
    result = _case(cases, "accepted_high_value_001")
    assert result["passed"]
    assert result["actual"]["submission_created"] is True
    assert result["actual"]["contribution_eligibilities"] == ["eligible"]
    assert result["actual"]["reputation_after"] > result["actual"]["reputation_before"]
    assert result["actual"]["reward_amounts"][0] > 0
    assert result["actual"]["submission_statuses"] == ["rewarded"]
    assert result["metrics"]["reward_events_created"] == 1


def test_duplicate_flow_is_negative_without_reward_or_penalty(benchmark):
    cases, _ = benchmark
    result = _case(cases, "duplicate_no_reward_001")
    assert result["actual"]["contribution_eligibilities"] == ["ineligible"]
    assert result["actual"]["contribution_scores"] == [0.0]
    assert result["actual"]["reputation_deltas"] == [-0.01]
    assert result["actual"]["reward_amounts"] == [0.0]
    assert result["actual"]["penalty_created"] is False
    assert result["metrics"]["reward_events_created"] == 0


def test_out_of_scope_flow_is_denied_without_penalty(benchmark):
    cases, _ = benchmark
    result = _case(cases, "out_of_scope_no_reward_001")
    assert result["actual"]["contribution_eligibilities"] == ["ineligible"]
    assert result["actual"]["reputation_deltas"] == [-0.02]
    assert result["actual"]["reward_amounts"] == [0.0]
    assert result["actual"]["penalty_created"] is False


def test_unsafe_flow_records_only_simulated_reviewable_penalty(benchmark):
    cases, _ = benchmark
    result = _case(cases, "unsafe_penalty_001")
    actual = result["actual"]
    assert actual["contribution_eligibilities"] == ["ineligible"]
    assert actual["reputation_deltas"] == [-0.08]
    assert actual["reward_amounts"] == [0.0]
    assert actual["penalty_created"] is True
    assert actual["protocol_penalty_points"] == 25
    assert actual["simulated_stake_loss"] == 0
    assert actual["executed_onchain"] is False
    assert actual["submission_statuses"] == ["penalized"]


def test_inactive_node_stops_before_downstream_records(benchmark):
    cases, _ = benchmark
    result = _case(cases, "inactive_node_rejected_001")
    assert result["actual"]["submission_rejected"] is True
    assert result["actual"]["submission_error_type"] == "NodeInactiveError"
    assert result["metrics"]["submissions_created"] == 0
    assert result["metrics"]["reputation_events_created"] == 0
    assert result["metrics"]["reward_cycles_created"] == 0
    assert result["metrics"]["penalty_events_created"] == 0


def test_reputation_growth_is_gradual_bounded_and_idempotent(benchmark):
    cases, _ = benchmark
    result = _case(cases, "reputation_growth_001")
    assert result["actual"]["reputation_deltas"] == [0.06, 0.06, 0.06]
    assert result["actual"]["reputation_after"] == 0.68
    assert result["actual"]["reputation_after"] <= 1.0
    assert result["actual"]["reputation_event_count"] == 3
    assert result["actual"]["node_statistics"]["total_submissions"] == 3
    assert result["actual"]["node_statistics"]["accepted_submissions"] == 3
    assert result["actual"]["idempotency_checks_passed"] == 3


def test_shared_reward_comparison_favors_high_value_and_preserves_pool(benchmark):
    _, leaderboard = benchmark
    comparison = leaderboard["reward_comparison"]
    assert comparison["passed"] is True
    assert comparison["high_raw_weight"] > comparison["medium_raw_weight"]
    assert comparison["high_reward"] > comparison["medium_reward"]
    assert comparison["total_allocated"] == comparison["reward_pool"]
    assert round(comparison["high_reward"] + comparison["medium_reward"], 6) == 1000.0


def test_all_required_idempotency_scenarios_pass(benchmark):
    _, leaderboard = benchmark
    assert leaderboard["idempotency_checks"] >= 5
    assert leaderboard["idempotency_checks_passed"] == leaderboard["idempotency_checks"]
