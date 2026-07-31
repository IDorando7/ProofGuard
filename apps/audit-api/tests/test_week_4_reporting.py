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


def _generate_outputs(tmp_path: Path):
    eval_validation.run_evaluation(results_dir=tmp_path)
    return (
        tmp_path / "validation_case_results.json",
        tmp_path / "validation_leaderboard.json",
        tmp_path / "week_4_report.md",
        tmp_path / "week_4_summary.json",
    )


def test_week_4_report_contains_validator_layer_title(tmp_path: Path):
    _, _, report_path, _ = _generate_outputs(tmp_path)

    assert "Validator Layer v0" in report_path.read_text(encoding="utf-8")


def test_week_4_report_contains_architecture_diagram_section(tmp_path: Path):
    _, _, report_path, _ = _generate_outputs(tmp_path)
    report = report_path.read_text(encoding="utf-8")

    assert "## 3. Architecture" in report
    assert "Candidate Finding" in report
    assert "Validator Pipeline" in report


def test_week_4_report_contains_validator_decision_rules(tmp_path: Path):
    _, _, report_path, _ = _generate_outputs(tmp_path)
    report = report_path.read_text(encoding="utf-8")

    assert "## 4. Validator decision rules" in report
    assert "Accepted requires" in report


def test_week_4_report_contains_benchmark_results_table(tmp_path: Path):
    _, _, report_path, _ = _generate_outputs(tmp_path)
    report = report_path.read_text(encoding="utf-8")

    assert "| Case | Expected | Actual | Passed | Notes |" in report
    assert "accepted_reproduced_001" in report
    assert "duplicate_finding_001" in report


def test_week_4_summary_contains_implemented_list(tmp_path: Path):
    _, _, _, summary_path = _generate_outputs(tmp_path)
    summary = _read_json(summary_path)

    assert "Synthetic validation benchmark" in summary["implemented"]


def test_week_4_summary_contains_metrics(tmp_path: Path):
    _, _, _, summary_path = _generate_outputs(tmp_path)
    summary = _read_json(summary_path)

    assert summary["metrics"]["total_validation_cases"] == 7
    assert summary["metrics"]["accuracy"] == 1.0


def test_week_4_summary_contains_limitations(tmp_path: Path):
    _, _, _, summary_path = _generate_outputs(tmp_path)
    summary = _read_json(summary_path)

    assert "Benchmark is synthetic and small." in summary["limitations"]


def test_week_4_summary_contains_next_week_focus(tmp_path: Path):
    _, _, _, summary_path = _generate_outputs(tmp_path)
    summary = _read_json(summary_path)

    assert "Protocol/reward model specification" in summary["next_week_focus"]
