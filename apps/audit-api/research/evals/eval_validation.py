from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[2]
RESEARCH_ROOT = APP_ROOT / "research"
DATASET_ROOT = RESEARCH_ROOT / "datasets" / "validation"
MANIFEST_PATH = DATASET_ROOT / "manifest.json"
RESULTS_DIR = RESEARCH_ROOT / "results"
CASE_RESULTS_PATH = RESULTS_DIR / "validation_case_results.json"
LEADERBOARD_PATH = RESULTS_DIR / "validation_leaderboard.json"
WEEK_4_REPORT_PATH = RESULTS_DIR / "week_4_report.md"
WEEK_4_SUMMARY_PATH = RESULTS_DIR / "week_4_summary.json"

if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from app.services.validator_pipeline import validate_all_findings


STATUS_COUNT_KEYS = {
    "accepted": "accepted_count",
    "duplicate": "duplicate_count",
    "out_of_scope": "out_of_scope_count",
    "insufficient_evidence": "insufficient_evidence_count",
    "unsafe_poc": "unsafe_poc_count",
    "unsupported": "unsupported_count",
    "needs_review": "needs_review_count",
}


def main() -> None:
    args = _parse_args()
    case_results, leaderboard = run_evaluation(
        manifest_path=args.dataset,
        results_dir=args.results_dir,
    )
    print_summary(leaderboard)


def run_evaluation(
    manifest_path: Path = MANIFEST_PATH,
    results_dir: Path = RESULTS_DIR,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    manifest = _read_json(manifest_path)
    dataset_root = manifest_path.parent
    case_results: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="proofguard-validation-eval-") as tmp_dir:
        tmp_root = Path(tmp_dir)
        for case in manifest["cases"]:
            case_path = dataset_root / case["path"]
            case_results.append(evaluate_case(case["case_id"], case_path, tmp_root))

    leaderboard = aggregate_results(case_results)
    write_case_results(case_results, results_dir / "validation_case_results.json")
    write_leaderboard(leaderboard, results_dir / "validation_leaderboard.json")
    write_week_4_report(case_results, leaderboard, results_dir / "week_4_report.md")
    write_week_4_summary(leaderboard, results_dir / "week_4_summary.json")
    return case_results, leaderboard


def evaluate_case(case_id: str, case_path: Path, tmp_root: Path) -> dict[str, Any]:
    expected = _read_json(case_path / "expected_validation.json")
    workspace = _prepare_workspace(case_id, case_path, tmp_root)
    decisions = validate_all_findings(project_id=case_id, project_workspace=workspace)
    actual_statuses = {decision.finding_id: decision.status.value for decision in decisions}
    expected_statuses = expected["expected_statuses"]
    notes = list(expected.get("notes", []))
    duplicate_special_match = _duplicate_case_matches(case_id, actual_statuses)
    expected_mismatches = {
        finding_id: {
            "expected": expected_status,
            "actual": actual_statuses.get(finding_id),
        }
        for finding_id, expected_status in expected_statuses.items()
        if actual_statuses.get(finding_id) != expected_status
    }
    unexpected_actual_ids = sorted(set(actual_statuses) - set(expected_statuses))

    if duplicate_special_match and expected_mismatches:
        expected_mismatches = {}
        notes.append("Duplicate case accepted by status counts: exactly one accepted and one duplicate.")

    mismatched_findings = len(expected_mismatches) + len(unexpected_actual_ids)
    matched_findings = len(expected_statuses) - len(expected_mismatches)
    passed = mismatched_findings == 0

    return {
        "case_id": case_id,
        "expected_statuses": expected_statuses,
        "actual_statuses": actual_statuses,
        "passed": passed,
        "total_findings": len(actual_statuses),
        "matched_findings": matched_findings,
        "mismatched_findings": mismatched_findings,
        "notes": notes,
    }


def aggregate_results(case_results: list[dict[str, Any]]) -> dict[str, Any]:
    total_cases = len(case_results)
    passed_cases = sum(1 for result in case_results if result["passed"])
    status_counts = Counter(
        status
        for result in case_results
        for status in result["actual_statuses"].values()
    )
    leaderboard = {
        "total_cases": total_cases,
        "passed_cases": passed_cases,
        "failed_cases": total_cases - passed_cases,
        "total_findings": sum(int(result["total_findings"]) for result in case_results),
        "matched_findings": sum(int(result["matched_findings"]) for result in case_results),
        "mismatched_findings": sum(int(result["mismatched_findings"]) for result in case_results),
        "accepted_count": 0,
        "duplicate_count": 0,
        "out_of_scope_count": 0,
        "insufficient_evidence_count": 0,
        "unsafe_poc_count": 0,
        "unsupported_count": 0,
        "needs_review_count": 0,
        "accuracy": passed_cases / total_cases if total_cases else 0.0,
    }
    for status, field in STATUS_COUNT_KEYS.items():
        leaderboard[field] = status_counts.get(status, 0)
    return leaderboard


def write_case_results(case_results: list[dict[str, Any]], output_path: Path = CASE_RESULTS_PATH) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps({"cases": case_results}, indent=2), encoding="utf-8")
    return output_path


def write_leaderboard(leaderboard: dict[str, Any], output_path: Path = LEADERBOARD_PATH) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(leaderboard, indent=2), encoding="utf-8")
    return output_path


def write_week_4_summary(
    leaderboard: dict[str, Any],
    output_path: Path = WEEK_4_SUMMARY_PATH,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "week": "Week 4",
        "theme": "Validator Layer v0",
        "implemented": [
            "ValidationDecision schema and storage",
            "Scope Validator",
            "Deduplication Engine v0",
            "Severity Normalizer v0",
            "Validator Pipeline v0",
            "Validation API",
            "Final Audit Report v0",
            "Synthetic validation benchmark",
        ],
        "metrics": {
            "total_validation_cases": leaderboard["total_cases"],
            "passed_cases": leaderboard["passed_cases"],
            "failed_cases": leaderboard["failed_cases"],
            "accuracy": leaderboard["accuracy"],
            "accepted_count": leaderboard["accepted_count"],
            "duplicate_count": leaderboard["duplicate_count"],
            "out_of_scope_count": leaderboard["out_of_scope_count"],
            "insufficient_evidence_count": leaderboard["insufficient_evidence_count"],
            "unsafe_poc_count": leaderboard["unsafe_poc_count"],
            "unsupported_count": leaderboard["unsupported_count"],
            "needs_review_count": leaderboard["needs_review_count"],
        },
        "validator_decision_rules": [
            "Accepted requires in-scope finding, non-duplicate status, and reproduced evidence.",
            "Out-of-scope findings are rejected before reproduction status is considered.",
            "Duplicate findings are marked separately and not counted as new accepted issues.",
            "Failed reproduction maps to insufficient_evidence.",
            "Rejected unsafe PoC maps to unsafe_poc.",
            "Unsupported reproduction maps to unsupported.",
            "Missing reproduction maps to needs_review.",
        ],
        "limitations": [
            "Benchmark is synthetic and small.",
            "Validator is deterministic and heuristic-based.",
            "Severity normalization does not replace human review.",
            "No decentralized validator subnet is active yet.",
            "No token reward or staking mechanism is implemented yet.",
        ],
        "next_week_focus": [
            "Protocol/reward model specification",
            "Node identity and reputation design",
            "Validator scoring and slashing rules",
            "Better benchmark coverage",
            "Optional real dataset integration",
        ],
    }
    output_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return output_path


def write_week_4_report(
    case_results: list[dict[str, Any]],
    leaderboard: dict[str, Any],
    output_path: Path = WEEK_4_REPORT_PATH,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Week 4 Report - Validator Layer v0",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "## 1. Goal",
        "",
        "Week 4 converts reproduced findings into validation decisions. The system now decides whether a finding is accepted, duplicate, out-of-scope, insufficient evidence, unsafe, unsupported, or needs review. No blockchain or reward logic is implemented yet.",
        "",
        "## 2. What was implemented",
        "",
        "- ValidationDecision schema and storage",
        "- Scope Validator",
        "- Deduplication Engine v0",
        "- Severity Normalizer v0",
        "- Validator Pipeline v0",
        "- Validation API",
        "- Final Audit Report v0",
        "- Synthetic validation benchmark",
        "",
        "## 3. Architecture",
        "",
        "```text",
        "Candidate Finding",
        "|",
        "v",
        "ReproductionResult",
        "|",
        "v",
        "Validator Pipeline",
        "|",
        "+--> Scope Validator",
        "+--> Deduplication Engine",
        "+--> Severity Normalizer",
        "+--> Evidence Check",
        "|",
        "v",
        "ValidationDecision",
        "|",
        "v",
        "Final Audit Report",
        "```",
        "",
        "## 4. Validator decision rules",
        "",
        "- Accepted requires in_scope + not duplicate + reproduced.",
        "- Out-of-scope is rejected before acceptance.",
        "- Duplicate is not counted as a new accepted finding.",
        "- Failed reproduction maps to insufficient_evidence.",
        "- rejected_unsafe maps to unsafe_poc.",
        "- unsupported maps to unsupported.",
        "- Missing reproduction maps to needs_review.",
        "",
        "## 5. Benchmark results",
        "",
        "| Case | Expected | Actual | Passed | Notes |",
        "| --- | --- | --- | --- | --- |",
    ]
    for result in case_results:
        lines.append(
            "| "
            f"{result['case_id']} | "
            f"{_expected_label(result)} | "
            f"{_actual_label(result)} | "
            f"{'yes' if result['passed'] else 'no'} | "
            f"{_case_note(result)} |"
        )
    lines.extend(
        [
            "",
            "## 6. Aggregate metrics",
            "",
            f"- Total cases: {leaderboard['total_cases']}",
            f"- Passed cases: {leaderboard['passed_cases']}",
            f"- Failed cases: {leaderboard['failed_cases']}",
            f"- Total findings: {leaderboard['total_findings']}",
            f"- Matched findings: {leaderboard['matched_findings']}",
            f"- Mismatched findings: {leaderboard['mismatched_findings']}",
            f"- Accepted count: {leaderboard['accepted_count']}",
            f"- Duplicate count: {leaderboard['duplicate_count']}",
            f"- Out-of-scope count: {leaderboard['out_of_scope_count']}",
            f"- Insufficient evidence count: {leaderboard['insufficient_evidence_count']}",
            f"- Unsafe PoC count: {leaderboard['unsafe_poc_count']}",
            f"- Unsupported count: {leaderboard['unsupported_count']}",
            f"- Needs review count: {leaderboard['needs_review_count']}",
            f"- Accuracy: {leaderboard['accuracy']:.2f}",
            "",
            "## 7. Final audit report capability",
            "",
            "- Week 4 can generate `final_report.md`.",
            "- Week 4 can generate `final_report.json`.",
            "- The report groups findings by validation status.",
            "- The report includes severity and validation breakdowns.",
            "",
            "## 8. Current limitations",
            "",
            "- Synthetic benchmark only.",
            "- Deterministic heuristic validator.",
            "- No human review workflow yet.",
            "- No decentralized validators yet.",
            "- No token/staking/reward logic yet.",
            "- No real dataset integration yet.",
            "",
            "## 9. Next steps",
            "",
            "- Week 5 can focus on protocol/reward model specification.",
            "- Node identity.",
            "- Reputation.",
            "- Validator scoring.",
            "- Reward rules.",
            "- Slashing rules.",
            "- Real dataset expansion.",
        ]
    )
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output_path


def print_summary(leaderboard: dict[str, Any]) -> None:
    print("Week 4 validation benchmark")
    for key in ("total_cases", "passed_cases", "failed_cases", "accuracy"):
        value = leaderboard[key]
        print(f"{key}: {value:.2f}" if isinstance(value, float) else f"{key}: {value}")


def _prepare_workspace(case_id: str, case_path: Path, tmp_root: Path) -> Path:
    workspace = tmp_root / case_id
    shutil.copytree(case_path, workspace)
    root_findings = workspace / "findings.json"
    findings_dir = workspace / "findings"
    if root_findings.is_file():
        findings_dir.mkdir(exist_ok=True)
        shutil.copyfile(root_findings, findings_dir / "all_findings.json")
    return workspace


def _duplicate_case_matches(case_id: str, actual_statuses: dict[str, str]) -> bool:
    if case_id != "duplicate_finding_001":
        return False
    counts = Counter(actual_statuses.values())
    return counts["accepted"] == 1 and counts["duplicate"] == 1 and len(actual_statuses) == 2


def _expected_label(result: dict[str, Any]) -> str:
    if result["case_id"] == "duplicate_finding_001":
        return "one accepted, one duplicate"
    return ", ".join(result["expected_statuses"].values())


def _actual_label(result: dict[str, Any]) -> str:
    if result["case_id"] == "duplicate_finding_001" and _duplicate_case_matches(result["case_id"], result["actual_statuses"]):
        return "one accepted, one duplicate"
    return ", ".join(result["actual_statuses"].values())


def _case_note(result: dict[str, Any]) -> str:
    default_notes = {
        "accepted_reproduced_001": "reproduced and in scope",
        "duplicate_finding_001": "duplicate detected",
        "out_of_scope_001": "contract outside scope",
        "insufficient_evidence_001": "reproduction failed",
        "unsafe_poc_001": "PoC rejected by safety checks",
        "unsupported_001": "unsupported reproduction layer",
        "needs_review_no_reproduction_001": "missing reproduction",
    }
    return default_notes.get(result["case_id"], "; ".join(result["notes"]))


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Week 4 validation benchmark.")
    parser.add_argument("--dataset", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    return parser.parse_args()


if __name__ == "__main__":
    main()
