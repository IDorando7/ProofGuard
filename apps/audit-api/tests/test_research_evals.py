import sys
from pathlib import Path

from app.schemas.finding import FindingCategory, FindingCreate, FindingSeverity

APP_ROOT = Path(__file__).resolve().parents[1]
EVALS_ROOT = APP_ROOT / "research" / "evals"
if str(EVALS_ROOT) not in sys.path:
    sys.path.insert(0, str(EVALS_ROOT))

from eval_common import (  # noqa: E402
    aggregate_agent_metrics,
    calculate_f1,
    calculate_precision,
    calculate_recall,
    calculate_score,
    compare_findings,
    count_false_negatives,
    count_positive_negative_cases,
    load_expected_findings,
    write_case_results,
    write_leaderboard,
)


def _finding(
    category: FindingCategory = FindingCategory.ACCESS_CONTROL,
    contract: str = "src/VulnerableVault.sol",
    function: str = "setTreasury",
) -> FindingCreate:
    return FindingCreate(
        title="Candidate benchmark finding",
        category=category,
        severity=FindingSeverity.HIGH,
        confidence=0.8,
        contracts=[contract],
        functions=[function],
        root_cause="Missing authorization for privileged function.",
        attack_path="An unauthorized user calls the function directly.",
        impact="Privileged protocol state can be changed.",
        recommended_fix="Add onlyOwner or role-based authorization.",
        agent_name="test_agent",
    )


def test_expected_findings_json_loads_correctly():
    case_path = APP_ROOT / "research" / "datasets" / "access_control" / "cases" / "access_001"

    expected = load_expected_findings(case_path)

    assert expected[0]["category"] == "access_control"
    assert expected[0]["contract"] == "src/VulnerableVault.sol"
    assert expected[0]["function"] == "setTreasury"


def test_negative_case_has_zero_expected_findings():
    case_path = (
        APP_ROOT
        / "research"
        / "datasets"
        / "reentrancy"
        / "cases"
        / "reentrancy_negative_001"
    )

    assert load_expected_findings(case_path) == []


def test_scoring_function_rewards_true_positives():
    assert calculate_score({"true_positives": 1}) == 5


def test_scoring_function_penalizes_false_positives():
    assert calculate_score({"false_positives": 1}) == -4


def test_false_negatives_are_calculated_correctly():
    assert count_false_negatives(expected_count=3, true_positives=1) == 2


def test_precision_is_calculated_correctly():
    assert calculate_precision(true_positives=2, false_positives=1) == 2 / 3


def test_recall_is_calculated_correctly():
    assert calculate_recall(true_positives=2, expected_findings=4) == 0.5
    assert calculate_recall(true_positives=0, expected_findings=0, false_positives=0) == 1.0
    assert calculate_recall(true_positives=0, expected_findings=0, false_positives=1) == 0.0


def test_f1_score_is_calculated_correctly():
    assert calculate_f1(precision=1.0, recall=0.5) == 2 / 3
    assert calculate_f1(precision=0.0, recall=0.0) == 0.0


def test_score_penalizes_false_negatives():
    assert calculate_score({"false_negatives": 1}) == -3


def test_score_penalizes_schema_invalid_findings():
    assert calculate_score({"schema_invalid_findings": 1}) == -2


def test_out_of_scope_finding_is_detected():
    metrics = compare_findings(
        [_finding(contract="src/OutOfScope.sol")],
        [],
        contracts_in_scope=["src/VulnerableVault.sol"],
    )

    assert metrics["out_of_scope_findings"] == 1


def test_schema_invalid_finding_is_detected():
    metrics = compare_findings(
        [
            {
                "title": "bad",
                "category": "access_control",
                "confidence": 0.5,
            }
        ],
        [],
        contracts_in_scope=["src/VulnerableVault.sol"],
    )

    assert metrics["schema_invalid_findings"] == 1


def test_duplicate_finding_is_detected():
    finding = _finding()

    metrics = compare_findings(
        [finding, finding],
        [
            {
                "category": "access_control",
                "contract": "src/VulnerableVault.sol",
                "function": "setTreasury",
                "root_cause_keywords": ["missing authorization"],
                "severity": "High",
            }
        ],
        contracts_in_scope=["src/VulnerableVault.sol"],
    )

    assert metrics["duplicate_count"] == 1


def test_positive_and_negative_cases_are_counted_correctly():
    positive_cases, negative_cases = count_positive_negative_cases(
        [
            {"positive_case": True},
            {"positive_case": True},
            {"positive_case": False},
        ]
    )

    assert positive_cases == 2
    assert negative_cases == 1


def test_case_results_json_is_written(tmp_path: Path):
    output_path = tmp_path / "case_results.json"

    write_case_results(
        {
            "access_control": [
                {
                    "case_id": "access_001",
                    "positive_case": True,
                    "expected_findings": 1,
                    "actual_findings": 1,
                    "true_positives": 1,
                    "false_positives": 0,
                    "false_negatives": 0,
                    "out_of_scope_findings": 0,
                    "duplicate_count": 0,
                    "schema_invalid_findings": 0,
                    "precision": 1.0,
                    "recall": 1.0,
                    "f1_score": 1.0,
                    "score": 5,
                }
            ]
        },
        output_path=output_path,
    )

    assert output_path.is_file()
    assert '"case_id": "access_001"' in output_path.read_text(encoding="utf-8")


def test_leaderboard_json_is_written(tmp_path: Path, monkeypatch):
    import eval_common

    leaderboard_path = tmp_path / "leaderboard.json"
    monkeypatch.setattr(eval_common, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(eval_common, "LEADERBOARD_PATH", leaderboard_path)

    write_leaderboard(
        {
            "access_control": {
                "total_cases": 3,
                "positive_cases": 2,
                "negative_cases": 1,
                "expected_findings": 2,
                "actual_findings": 2,
                "true_positives": 2,
                "false_positives": 0,
                "false_negatives": 0,
                "out_of_scope_findings": 0,
                "duplicate_count": 0,
                "schema_invalid_findings": 0,
                "precision": 1.0,
                "recall": 1.0,
                "f1_score": 1.0,
                "score": 10,
            }
        }
    )

    assert leaderboard_path.is_file()
    assert '"access_control"' in leaderboard_path.read_text(encoding="utf-8")


def test_leaderboard_contains_new_and_old_fields(tmp_path: Path):
    output_path = tmp_path / "leaderboard.json"
    summary = aggregate_agent_metrics(
        [
            {
                "case_id": "access_001",
                "positive_case": True,
                "expected_findings": 1,
                "actual_findings": 1,
                "true_positives": 1,
                "false_positives": 0,
                "false_negatives": 0,
                "out_of_scope_findings": 0,
                "duplicate_count": 0,
                "schema_invalid_findings": 0,
                "precision": 1.0,
                "recall": 1.0,
                "f1_score": 1.0,
                "score": 5,
            },
            {
                "case_id": "access_negative_001",
                "positive_case": False,
                "expected_findings": 0,
                "actual_findings": 0,
                "true_positives": 0,
                "false_positives": 0,
                "false_negatives": 0,
                "out_of_scope_findings": 0,
                "duplicate_count": 0,
                "schema_invalid_findings": 0,
                "precision": 0.0,
                "recall": 1.0,
                "f1_score": 0.0,
                "score": 0,
            },
        ]
    )

    write_leaderboard({"access_control": summary}, output_path=output_path)
    content = output_path.read_text(encoding="utf-8")

    assert '"positive_cases": 1' in content
    assert '"negative_cases": 1' in content
    assert '"expected_findings": 1' in content
    assert '"actual_findings": 1' in content
    assert '"false_negatives": 0' in content
    assert '"schema_invalid_findings": 0' in content
    assert '"precision": 1.0' in content
    assert '"recall": 1.0' in content
    assert '"f1_score": 1.0' in content
    assert '"total_cases": 2' in content
    assert '"true_positives": 1' in content
    assert '"false_positives": 0' in content
    assert '"out_of_scope_findings": 0' in content
    assert '"duplicate_count": 0' in content
    assert '"score": 5' in content
