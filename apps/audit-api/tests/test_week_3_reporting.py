import json
import sys
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
EVALS_ROOT = APP_ROOT / "research" / "evals"
if str(EVALS_ROOT) not in sys.path:
    sys.path.insert(0, str(EVALS_ROOT))

import eval_reproduction  # noqa: E402


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _generate_outputs(tmp_path: Path):
    case_results, leaderboard = eval_reproduction.run_evaluation(mock_sandbox=True)
    eval_reproduction.write_results(case_results, leaderboard, results_dir=tmp_path)
    summary_path = eval_reproduction.write_week_3_summary(leaderboard, output_path=tmp_path / "week_3_summary.json")
    report_path = eval_reproduction.write_week_3_report(
        case_results,
        leaderboard,
        output_path=tmp_path / "week_3_report.md",
    )
    reproduction_report_path = eval_reproduction.write_report(
        case_results,
        leaderboard,
        mock_sandbox=True,
        report_path=tmp_path / "week_3_reproduction_report.md",
    )
    return case_results, leaderboard, summary_path, report_path, reproduction_report_path


def test_week_3_summary_json_is_generated(tmp_path: Path):
    _, _, summary_path, _, _ = _generate_outputs(tmp_path)

    assert summary_path.is_file()


def test_week_3_summary_contains_implemented_security_controls_and_limitations(tmp_path: Path):
    _, _, summary_path, _, _ = _generate_outputs(tmp_path)
    summary = _read_json(summary_path)

    assert summary["implemented"]
    assert "Safety Preflight" in summary["implemented"]
    assert summary["security_controls"]
    assert "No shell=True" in summary["security_controls"]
    assert summary["limitations"]
    assert "Benchmark is synthetic and small" in summary["limitations"]


def test_week_3_report_is_generated(tmp_path: Path):
    _, _, _, report_path, _ = _generate_outputs(tmp_path)

    assert report_path.is_file()


def test_week_3_report_contains_architecture_diagram_section(tmp_path: Path):
    _, _, _, report_path, _ = _generate_outputs(tmp_path)
    report = report_path.read_text(encoding="utf-8")

    assert "## 3. Architecture" in report
    assert "Candidate Finding" in report
    assert "SandboxedCommandRunner" in report


def test_week_3_report_contains_benchmark_table_and_security_controls(tmp_path: Path):
    _, _, _, report_path, _ = _generate_outputs(tmp_path)
    report = report_path.read_text(encoding="utf-8")

    assert "| Case | Expected | Actual | Passed | Notes |" in report
    assert "access_control_poc_001" in report
    assert "## 4. Security controls" in report
    assert "No `shell=True`" in report


def test_reproduction_case_results_include_new_fields(tmp_path: Path):
    _generate_outputs(tmp_path)
    case_results = _read_json(tmp_path / "reproduction_case_results.json")

    for case in case_results["cases"]:
        assert "safety_issues_count" in case
        assert "safety_high_or_critical_count" in case
        assert "execution_mode" in case
        assert "failure_reason" in case


def test_reproduction_leaderboard_contains_required_fields(tmp_path: Path):
    _generate_outputs(tmp_path)
    leaderboard = _read_json(tmp_path / "reproduction_leaderboard.json")

    for field in [
        "total_cases",
        "passed_cases",
        "failed_cases",
        "reproduction_attempts",
        "reproduced_count",
        "rejected_unsafe_count",
        "unsupported_count",
        "accuracy",
    ]:
        assert field in leaderboard


def test_eval_reproduction_mock_sandbox_writes_all_default_outputs(monkeypatch):
    monkeypatch.setattr(
        eval_reproduction.sandbox_runner,
        "run_in_sandbox",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("Docker should not be called in mock mode")),
    )

    case_results, leaderboard = eval_reproduction.run_evaluation(mock_sandbox=True)
    eval_reproduction.write_results(case_results, leaderboard)
    eval_reproduction.write_report(case_results, leaderboard, mock_sandbox=True)
    eval_reproduction.write_week_3_summary(leaderboard)
    eval_reproduction.write_week_3_report(case_results, leaderboard)

    assert (APP_ROOT / "research" / "results" / "reproduction_case_results.json").is_file()
    assert (APP_ROOT / "research" / "results" / "reproduction_leaderboard.json").is_file()
    assert (APP_ROOT / "research" / "results" / "week_3_reproduction_report.md").is_file()
    assert (APP_ROOT / "research" / "results" / "week_3_summary.json").is_file()
    assert (APP_ROOT / "research" / "results" / "week_3_report.md").is_file()

