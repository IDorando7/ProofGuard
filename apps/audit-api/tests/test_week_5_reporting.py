import json
import sys
from pathlib import Path

import pytest


APP_ROOT = Path(__file__).resolve().parents[1]
EVAL_ROOT = APP_ROOT / "research" / "evals"
if str(EVAL_ROOT) not in sys.path:
    sys.path.insert(0, str(EVAL_ROOT))

import eval_protocol  # noqa: E402


@pytest.fixture()
def outputs(tmp_path):
    eval_protocol.run_protocol_benchmark(results_dir=tmp_path)
    return tmp_path


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_case_results_contain_all_cases_checks_and_actuals(outputs):
    document = _json(outputs / "protocol_case_results.json")
    assert document["dataset"] == "week_5_protocol_synthetic"
    assert len(document["cases"]) == 7
    assert all("passed" in result and result["checks"] and result["actual"] for result in document["cases"])


def test_leaderboard_contains_required_bounded_metrics(outputs):
    leaderboard = _json(outputs / "protocol_leaderboard.json")
    required = {
        "total_cases", "passed_cases", "failed_cases", "pass_rate", "submissions_created",
        "submissions_rejected", "eligible_contributions", "ineligible_contributions",
        "reputation_events_created", "positive_reputation_events", "negative_reputation_events",
        "reward_cycles_created", "reward_cycles_finalized", "reward_events_created",
        "zero_reward_submissions", "penalty_events_created", "idempotency_checks",
        "idempotency_checks_passed", "total_protocol_points_distributed", "accuracy",
    }
    assert required <= leaderboard.keys()
    assert 0 <= leaderboard["pass_rate"] <= 1
    assert 0 <= leaderboard["accuracy"] <= 1
    assert leaderboard["total_protocol_points_distributed"] >= 0


def test_summary_contains_required_week_5_sections(outputs):
    summary = _json(outputs / "week_5_summary.json")
    assert summary["week"] == "Week 5"
    for field in (
        "implemented", "protocol_flow", "metrics", "protocol_invariants_verified",
        "security_properties", "limitations", "next_week_focus",
    ):
        assert summary[field]


def test_report_contains_all_required_sections(outputs):
    report = (outputs / "week_5_report.md").read_text(encoding="utf-8")
    assert report.startswith("# Week 5 Report - Protocol and Incentive Layer v0")
    for section in (
        "## 3. Protocol architecture",
        "## 6. Contribution scoring",
        "## 7. Reputation model",
        "## 8. Reward-cycle model",
        "## 9. Penalty model",
        "## 10. Benchmark cases",
        "## 11. Aggregate metrics",
        "## 13. Idempotency verification",
        "## 14. Security properties",
        "## 16. Week 6 direction",
    ):
        assert section in report
    assert "accepted_high_value_001" in report
    assert "unsafe_penalty_001" in report


def test_report_describes_absent_capabilities_truthfully(outputs):
    report = (outputs / "week_5_report.md").read_text(encoding="utf-8").lower()
    assert "protocol_points are not tokens" in report
    assert "no blockchain or real token exists" in report
    assert "no category subnets exist" in report
    assert "no stake was locked or reduced" in report
    assert "no blockchain operation" in report
