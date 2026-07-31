import inspect
import json
import sys
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
EVALS_ROOT = APP_ROOT / "research" / "evals"
if str(EVALS_ROOT) not in sys.path:
    sys.path.insert(0, str(EVALS_ROOT))

import eval_validation  # noqa: E402


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_eval_validation_runs_without_docker():
    source = inspect.getsource(eval_validation)

    assert "docker" not in source
    assert "run_in_sandbox" not in source
    assert "subprocess" not in source


def test_eval_validation_writes_case_results(tmp_path: Path):
    eval_validation.run_evaluation(results_dir=tmp_path)

    assert (tmp_path / "validation_case_results.json").is_file()


def test_eval_validation_writes_leaderboard(tmp_path: Path):
    eval_validation.run_evaluation(results_dir=tmp_path)

    assert (tmp_path / "validation_leaderboard.json").is_file()


def test_eval_validation_writes_week_4_report(tmp_path: Path):
    eval_validation.run_evaluation(results_dir=tmp_path)

    assert (tmp_path / "week_4_report.md").is_file()


def test_eval_validation_writes_week_4_summary(tmp_path: Path):
    eval_validation.run_evaluation(results_dir=tmp_path)

    assert (tmp_path / "week_4_summary.json").is_file()


def test_validation_leaderboard_contains_required_fields(tmp_path: Path):
    eval_validation.run_evaluation(results_dir=tmp_path)
    leaderboard = _read_json(tmp_path / "validation_leaderboard.json")

    for field in [
        "total_cases",
        "passed_cases",
        "failed_cases",
        "total_findings",
        "matched_findings",
        "mismatched_findings",
        "accepted_count",
        "duplicate_count",
        "out_of_scope_count",
        "insufficient_evidence_count",
        "unsafe_poc_count",
        "unsupported_count",
        "needs_review_count",
        "accuracy",
    ]:
        assert field in leaderboard


def test_eval_validation_produces_expected_accuracy(tmp_path: Path):
    _, leaderboard = eval_validation.run_evaluation(results_dir=tmp_path)

    assert leaderboard["total_cases"] == 7
    assert leaderboard["passed_cases"] == 7
    assert leaderboard["accuracy"] == 1.0
