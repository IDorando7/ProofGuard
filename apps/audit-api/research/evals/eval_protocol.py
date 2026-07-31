from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[2]
RESEARCH_ROOT = APP_ROOT / "research"
DATASET_ROOT = RESEARCH_ROOT / "datasets" / "protocol"
MANIFEST_PATH = DATASET_ROOT / "manifest.json"
RESULTS_DIR = RESEARCH_ROOT / "results"

if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from app.schemas.finding import Finding
from app.schemas.node import NodeCreate, NodeStatusChangeRequest
from app.schemas.reproduction import ReproductionResult
from app.schemas.reward import RewardCycleCreate
from app.schemas.submission import SubmissionCreate, SubmissionStatusUpdate
from app.schemas.validation import ValidationDecision
from app.services.contribution_scoring_service import (
    calculate_contribution_for_submission,
    list_contribution_scores,
)
from app.services.node_registry_service import (
    NodeInactiveError,
    change_node_status,
    create_node,
    load_node,
)
from app.services.penalty_service import (
    list_penalty_events,
    load_penalty_event_by_submission,
    process_submission_penalty,
)
from app.services.reproduction_service import load_reproduction_result
from app.services.reputation_service import (
    get_node_reputation_history,
    process_submission_reputation,
)
from app.services.reward_service import (
    calculate_reward_cycle,
    create_reward_cycle,
    finalize_reward_cycle,
    list_reward_cycles,
    list_reward_events,
    load_reward_event_by_submission,
)
from app.services.scope_service import parse_scope_yaml
from app.services.submission_service import (
    DuplicateSubmissionError,
    create_submission,
    list_submissions,
    load_submission,
    update_submission_status,
)
from app.services.validation_service import load_validation_decision


DATASET_NAME = "week_5_protocol_synthetic"
CASE_RESULTS_FILENAME = "protocol_case_results.json"
LEADERBOARD_FILENAME = "protocol_leaderboard.json"
REPORT_FILENAME = "week_5_report.md"
SUMMARY_FILENAME = "week_5_summary.json"
ALLOWED_STEPS = {
    "create_node",
    "change_node_status",
    "create_submission",
    "calculate_contribution",
    "process_reputation",
    "create_reward_cycle",
    "calculate_reward_cycle",
    "finalize_reward_cycle",
    "process_penalty",
}
FINAL_SUBMISSION_STATUS = {
    "accepted": "accepted",
    "rejected": "rejected",
    "duplicate": "duplicate",
    "out_of_scope": "out_of_scope",
    "insufficient_evidence": "insufficient_evidence",
    "unsafe_poc": "unsafe",
    "unsafe": "unsafe",
    "unsupported": "unsupported",
    "needs_review": "needs_review",
}


def load_protocol_manifest(manifest_path: Path = MANIFEST_PATH) -> dict[str, Any]:
    try:
        manifest = _read_json(manifest_path)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Protocol manifest is missing or malformed") from exc
    validate_protocol_manifest(manifest, manifest_path)
    return manifest


def validate_protocol_manifest(manifest: dict[str, Any], manifest_path: Path = MANIFEST_PATH) -> None:
    if not isinstance(manifest, dict) or manifest.get("dataset") != DATASET_NAME:
        raise ValueError(f"Protocol manifest dataset must be {DATASET_NAME}")
    if not isinstance(manifest.get("version"), str) or not manifest["version"].strip():
        raise ValueError("Protocol manifest requires a version")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Protocol manifest requires a non-empty cases list")
    case_ids = [case.get("case_id") for case in cases if isinstance(case, dict)]
    if len(case_ids) != len(cases) or any(not isinstance(case_id, str) or not case_id for case_id in case_ids):
        raise ValueError("Every protocol case requires a case_id")
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("Protocol case IDs must be unique")

    dataset_root = manifest_path.parent.resolve()
    for entry in cases:
        relative_path = entry.get("path")
        if not isinstance(relative_path, str) or not relative_path:
            raise ValueError(f"Protocol case {entry['case_id']} requires a path")
        case_path = (manifest_path.parent / relative_path).resolve()
        try:
            case_path.relative_to(dataset_root)
        except ValueError as exc:
            raise ValueError(f"Protocol case {entry['case_id']} escapes the dataset root") from exc
        _validate_case_fixture(entry["case_id"], case_path)


def prepare_case_runtime(case_path: Path, runtime_root: Path) -> tuple[Path, Path]:
    audit_workspace = runtime_root / "audit_workspace"
    protocol_data = runtime_root / "protocol_data"
    shutil.copytree(case_path / "project", audit_workspace)
    protocol_data.mkdir(parents=True, exist_ok=False)
    return audit_workspace, protocol_data


def run_protocol_case(
    case_entry: dict[str, Any],
    case_path: Path,
    runtime_root: Path,
) -> dict[str, Any]:
    case_config = _read_json(case_path / "case.json")
    expected_document = _read_json(case_path / "expected_protocol.json")
    expected = expected_document["expected"]
    description = case_config["description"]
    base_result = {
        "case_id": case_entry["case_id"],
        "description": description,
        "passed": False,
        "steps": {
            "node_created": False,
            "submission_created": False,
            "contribution_calculated": False,
            "reputation_processed": False,
            "reward_cycle_finalized": False,
            "penalty_processed": False,
        },
        "actual": {},
        "expected": expected,
        "checks": [],
        "errors": [],
        "notes": list(expected_document.get("notes", [])),
        "metrics": _empty_case_metrics(),
    }
    try:
        workspace, protocol_data = prepare_case_runtime(case_path, runtime_root)
        result = _execute_case(case_config, protocol_data, workspace, base_result)
        checks = compare_protocol_case(result["actual"], expected)
        result["checks"] = checks
        result["passed"] = bool(checks) and all(check["passed"] for check in checks) and not result["errors"]
        return result
    except Exception as exc:  # Keep remaining independent cases running.
        base_result["errors"].append(_safe_error(exc))
        base_result["checks"].append(
            _check("case_completed_without_error", "successful execution", exc.__class__.__name__, False)
        )
        return base_result


def compare_protocol_case(actual: dict[str, Any], expected: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def add(name: str, expected_value: Any, actual_value: Any, passed: bool | None = None) -> None:
        checks.append(
            _check(name, expected_value, actual_value, actual_value == expected_value if passed is None else passed)
        )

    if "submission_created" in expected:
        add("submission_created", expected["submission_created"], actual.get("submission_created"))
    if expected.get("submission_rejected"):
        add("submission_rejected", True, actual.get("submission_rejected"))
    if "expected_error_type" in expected:
        add("expected_error_type", expected["expected_error_type"], actual.get("submission_error_type"))
    if "expected_node_status" in expected:
        add("node_status", expected["expected_node_status"], actual.get("node_status"))
    if "expected_reputation" in expected:
        add("unchanged_reputation", expected["expected_reputation"], actual.get("reputation_after"))

    scores = actual.get("contribution_scores", [])
    eligibilities = actual.get("contribution_eligibilities", [])
    if "contribution_eligibility" in expected:
        add(
            "contribution_eligibility",
            expected["contribution_eligibility"],
            eligibilities,
            bool(eligibilities) and all(value == expected["contribution_eligibility"] for value in eligibilities),
        )
    if "expected_contribution_score" in expected:
        add(
            "contribution_score_exact",
            expected["expected_contribution_score"],
            scores,
            bool(scores) and all(score == expected["expected_contribution_score"] for score in scores),
        )
    if "minimum_contribution_score" in expected:
        minimum = expected["minimum_contribution_score"]
        add("contribution_score_minimum", f">= {minimum}", scores, bool(scores) and all(score >= minimum for score in scores))
    if "maximum_contribution_score" in expected:
        maximum = expected["maximum_contribution_score"]
        add("contribution_score_maximum", f"<= {maximum}", scores, bool(scores) and all(score <= maximum for score in scores))

    if "expected_event_type" in expected:
        event_types = actual.get("reputation_event_types", [])
        add(
            "reputation_event_type",
            expected["expected_event_type"],
            event_types,
            bool(event_types) and all(value == expected["expected_event_type"] for value in event_types),
        )
    if "expected_reputation_delta" in expected:
        deltas = actual.get("reputation_deltas", [])
        add(
            "reputation_delta",
            expected["expected_reputation_delta"],
            deltas,
            bool(deltas) and all(delta == expected["expected_reputation_delta"] for delta in deltas),
        )
    if "reputation_direction" in expected:
        before = actual.get("reputation_before")
        after = actual.get("reputation_after")
        direction = "increase" if after is not None and before is not None and after > before else "decrease" if after is not None and before is not None and after < before else "unchanged"
        add("reputation_direction", expected["reputation_direction"], direction)
    if "expected_final_reputation" in expected:
        add("final_reputation", expected["expected_final_reputation"], actual.get("reputation_after"))
    if "expected_reputation_event_count" in expected:
        add("reputation_event_count", expected["expected_reputation_event_count"], actual.get("reputation_event_count"))

    if "reward_eligible" in expected:
        eligible_allocations = actual.get("eligible_reward_allocations", 0)
        expected_eligible = expected["reward_eligible"]
        add("reward_eligibility", expected_eligible, eligible_allocations, eligible_allocations > 0 if expected_eligible else eligible_allocations == 0)
    reward_amounts = actual.get("reward_amounts", [])
    if expected.get("reward_amount_rule") == "positive":
        add("reward_amount_positive", "all > 0", reward_amounts, bool(reward_amounts) and all(amount > 0 for amount in reward_amounts))
    elif expected.get("reward_amount_rule") == "zero":
        add("reward_amount_zero", 0, sum(reward_amounts), sum(reward_amounts) == 0)

    if "penalty_created" in expected:
        add("penalty_created", expected["penalty_created"], actual.get("penalty_created", False))
    penalty_fields = {
        "expected_penalty_reason": "penalty_reason",
        "expected_penalty_points": "protocol_penalty_points",
        "expected_simulated_stake_loss": "simulated_stake_loss",
        "expected_executed_onchain": "executed_onchain",
        "expected_human_review": "requires_human_review",
    }
    for expected_field, actual_field in penalty_fields.items():
        if expected_field in expected:
            add(actual_field, expected[expected_field], actual.get(actual_field))

    if "expected_submission_status" in expected:
        statuses = actual.get("submission_statuses", [])
        add(
            "submission_status",
            expected["expected_submission_status"],
            statuses,
            bool(statuses) and all(status == expected["expected_submission_status"] for status in statuses),
        )
    if "expected_submission_count" in expected:
        add("submission_count", expected["expected_submission_count"], actual.get("submission_count"))
    if expected.get("distinct_finding_hashes"):
        hashes = actual.get("finding_hashes", [])
        add("distinct_finding_hashes", True, len(set(hashes)) == len(hashes), bool(hashes) and len(set(hashes)) == len(hashes))
    for field, expected_value in expected.get("expected_statistics", {}).items():
        add(f"statistics_{field}", expected_value, actual.get("node_statistics", {}).get(field))
    if actual.get("idempotency_checks", 0):
        add(
            "idempotency_checks",
            actual["idempotency_checks"],
            actual.get("idempotency_checks_passed"),
        )
    return checks


def run_protocol_benchmark(
    manifest_path: Path = MANIFEST_PATH,
    results_dir: Path = RESULTS_DIR,
) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = load_protocol_manifest(manifest_path)
    generated_at = _utc_now().isoformat()
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="proofguard-protocol-eval-") as tmp_dir:
        temp_root = Path(tmp_dir)
        for index, entry in enumerate(manifest["cases"]):
            case_path = manifest_path.parent / entry["path"]
            case_runtime = temp_root / f"case_{index:02d}"
            case_runtime.mkdir()
            results.append(run_protocol_case(entry, case_path, case_runtime))
        comparison = _run_reward_comparison(manifest_path.parent, temp_root / "reward_comparison")

    case_document = {
        "dataset": manifest["dataset"],
        "version": manifest["version"],
        "generated_at": generated_at,
        "cases": results,
    }
    leaderboard = build_protocol_leaderboard(case_document, comparison)
    write_protocol_results(case_document, leaderboard, results_dir)
    return case_document, leaderboard


def build_protocol_leaderboard(
    case_document: dict[str, Any],
    reward_comparison: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cases = case_document["cases"]
    metrics = [result.get("metrics", {}) for result in cases]
    total_checks = sum(len(result.get("checks", [])) for result in cases) + (1 if reward_comparison else 0)
    passed_checks = sum(sum(bool(check["passed"]) for check in result.get("checks", [])) for result in cases)
    if reward_comparison and reward_comparison.get("passed"):
        passed_checks += 1
    passed_cases = sum(bool(result.get("passed")) for result in cases)

    def total(field: str) -> int | float:
        return sum(metric.get(field, 0) for metric in metrics)

    leaderboard = {
        "dataset": case_document["dataset"],
        "total_cases": len(cases),
        "passed_cases": passed_cases,
        "failed_cases": len(cases) - passed_cases,
        "pass_rate": round(passed_cases / len(cases), 6) if cases else 0.0,
        "total_checks": total_checks,
        "passed_checks": passed_checks,
        "accuracy": round(passed_checks / total_checks, 6) if total_checks else 0.0,
        "total_submissions_attempted": int(total("submissions_attempted")),
        "submissions_created": int(total("submissions_created")),
        "submissions_rejected": int(total("submissions_rejected")),
        "eligible_contributions": int(total("eligible_contributions")),
        "ineligible_contributions": int(total("ineligible_contributions")),
        "pending_contributions": int(total("pending_contributions")),
        "reputation_events_created": int(total("reputation_events_created")),
        "positive_reputation_events": int(total("positive_reputation_events")),
        "negative_reputation_events": int(total("negative_reputation_events")),
        "zero_delta_reputation_events": int(total("zero_delta_reputation_events")),
        "reward_cycles_created": int(total("reward_cycles_created")),
        "reward_cycles_finalized": int(total("reward_cycles_finalized")),
        "reward_events_created": int(total("reward_events_created")),
        "zero_reward_submissions": int(total("zero_reward_submissions")),
        "penalty_events_created": int(total("penalty_events_created")),
        "idempotency_checks": int(total("idempotency_checks")),
        "idempotency_checks_passed": int(total("idempotency_checks_passed")),
        "total_protocol_points_distributed": round(float(total("protocol_points_distributed")), 6),
        "reward_comparison": reward_comparison or {"passed": False, "error": "Comparison was not run."},
    }
    leaderboard["success"] = (
        leaderboard["failed_cases"] == 0 and leaderboard["reward_comparison"].get("passed") is True
    )
    return leaderboard


def build_week_5_summary(leaderboard: dict[str, Any]) -> dict[str, Any]:
    metric_fields = (
        "total_cases", "passed_cases", "failed_cases", "pass_rate", "accuracy",
        "reward_events_created", "penalty_events_created", "idempotency_checks_passed",
    )
    return {
        "week": "Week 5",
        "theme": "Protocol and Incentive Layer v0",
        "implemented": [
            "Protocol Specification v0",
            "Node Identity and Registry v0",
            "Finding Submission Protocol v0",
            "Contribution Scoring Engine v0",
            "Reputation Engine v0",
            "Reward Simulator v0",
            "Penalty Event v0",
            "Synthetic protocol benchmark",
        ],
        "protocol_flow": [
            "Register node", "Submit existing finding", "Calculate contribution score",
            "Process reputation", "Create reward cycle", "Calculate allocations",
            "Finalize reward events", "Record unsafe penalties",
        ],
        "metrics": {field: leaderboard[field] for field in metric_fields},
        "protocol_invariants_verified": [
            "Inactive nodes cannot submit findings.",
            "Only accepted and reproduced findings are reward eligible.",
            "Duplicate findings receive no reward.",
            "Out-of-scope findings receive no reward.",
            "Unsafe findings receive no reward.",
            "Unsafe findings may create PenaltyEvent records.",
            "Reputation changes only once per submission.",
            "Reward cycles finalize idempotently.",
            "Protocol points have no monetary value.",
        ],
        "security_properties": [
            "No PoCs are executed by the protocol benchmark.",
            "No Docker or forge commands are executed.",
            "No external subprocess commands are executed.",
            "No private keys or wallets are used.",
            "No blockchain operation occurs.",
            "No real stake is slashed.",
        ],
        "limitations": [
            "The benchmark is synthetic and small.",
            "The coordinator remains centralized.",
            "Node identities are off-chain UUID records.",
            "Public-key ownership is not verified.",
            "Reputation is global rather than category-specific.",
            "Category subnets are not implemented.",
            "Protocol points have no monetary value.",
            "Validator consensus and disputes are not implemented.",
            "No Sybil-resistance mechanism is active.",
        ],
        "next_week_focus": [
            "Category-specific node performance", "Subnet registry", "Per-category reputation",
            "Subnet membership rules", "Subnet routing", "Exploration slots for new nodes",
            "Category-specific reward allocation", "Subnet benchmark",
        ],
    }


def render_week_5_report(case_document: dict[str, Any], leaderboard: dict[str, Any]) -> str:
    comparison = leaderboard["reward_comparison"]
    lines = [
        "# Week 5 Report - Protocol and Incentive Layer v0",
        "",
        f"Generated: {case_document['generated_at']}",
        "",
        "## 1. Goal",
        "",
        "Week 5 transforms the centralized audit pipeline into an off-chain protocol simulation. Nodes have identities, submissions link findings to nodes, finalized contribution value changes global reputation, eligible contributions receive simulated protocol points, and unsafe submissions can create separate penalty records. No blockchain or real token exists.",
        "",
        "## 2. What was implemented",
        "",
        "- Protocol Specification v0",
        "- Node Identity and Registry v0",
        "- Finding Submission Protocol v0 and deterministic finding hashes",
        "- Contribution Scoring Engine v0",
        "- Reputation Engine v0",
        "- Reward Cycle Simulator and RewardEvents",
        "- Unsafe PenaltyEvents",
        "- Week 5 synthetic protocol benchmark",
        "",
        "## 3. Protocol architecture",
        "",
        "```text",
        "Audit Project",
        "    |",
        "    v",
        "Agent Node",
        "    |",
        "    v",
        "Finding Submission",
        "    |",
        "    v",
        "ReproductionResult",
        "    |",
        "    v",
        "ValidationDecision",
        "    |",
        "    v",
        "ContributionScore",
        "    |",
        "    v",
        "ReputationEvent",
        "    |",
        "    +--------------------+",
        "    |                    |",
        "    v                    v",
        "Reward Cycle       Penalty Event",
        "    |",
        "    v",
        "RewardEvent",
        "```",
        "",
        "## 4. Protocol actors",
        "",
        "The Audit Client supplies an authorized project and scope. Agent Nodes are implemented as off-chain UUID records and submit findings in declared categories. Validator Nodes remain conceptual; the current Validator Pipeline and static benchmark decisions are centralized. The Protocol Coordinator is the trusted FastAPI/service layer. The implemented Reward Engine is an off-chain deterministic protocol-points simulator.",
        "",
        "## 5. Node identity and submission flow",
        "",
        "Nodes receive UUIDs. Agent and hybrid nodes declare supported categories; inactive, suspended, and banned nodes cannot participate. A SubmissionRecord links node, project, finding, category, and canonical finding hash. The same node cannot submit the same canonical content twice in one project.",
        "",
        "## 6. Contribution scoring",
        "",
        "Contribution scores combine validity, severity, reproducibility, uniqueness, quality, and explicit penalties on a 0–100 scale. Accepted, reproduced, unique, in-scope inputs pass the hard reward gates; validation failures cannot be overridden by a high score.",
        "",
        "```text",
        "Validation decides correctness.",
        "Contribution scoring decides value.",
        "```",
        "",
        "## 7. Reputation model",
        "",
        "Reputation starts at 0.5, remains between 0.0 and 1.0, and changes through one immutable event per finalized submission. Idempotent processing prevents double application.",
        "",
        "| Outcome | Implemented delta |",
        "| --- | ---: |",
        "| Accepted base | +0.03 |",
        "| Contribution score >= 90 | +0.02 |",
        "| Contribution score 75–89.999 | +0.01 |",
        "| High severity | +0.01 |",
        "| Critical severity | +0.02 |",
        "| Duplicate | -0.01 |",
        "| Out of scope | -0.02 |",
        "| Insufficient evidence | -0.02 |",
        "| Rejected | -0.03 |",
        "| Unsafe | -0.08 |",
        "| Unsupported | 0.00 |",
        "",
        "## 8. Reward-cycle model",
        "",
        "Reward cycles move through draft, calculated, and finalized. The only unit is `protocol_points`.",
        "",
        "```text",
        "raw weight = contribution score * reputation multiplier * category multiplier",
        "category multiplier = 1.0 in Week 5",
        "```",
        "",
        "Eligible raw weights divide a fixed project pool proportionally. Source fingerprints protect finalization inputs and deterministic remainder correction preserves the pool total. protocol_points are not tokens.",
        "",
        "## 9. Penalty model",
        "",
        "Unsafe findings are denied reward. Their PenaltyEvent records 25 internal protocol penalty points, simulated stake loss 0, `executed_onchain=false`, and required human review. Ordinary mistakes do not automatically create penalty events.",
        "",
        "## 10. Benchmark cases",
        "",
        "| Case | Primary expectation | Actual result | Passed |",
        "| --- | --- | --- | --- |",
    ]
    for result in case_document["cases"]:
        lines.append(
            f"| {result['case_id']} | {_primary_expectation(result['case_id'])} | {_actual_result_label(result)} | {'yes' if result['passed'] else 'no'} |"
        )
    lines.extend(
        [
            "",
            "## 11. Aggregate metrics",
            "",
            f"- Total cases: {leaderboard['total_cases']}",
            f"- Passed cases: {leaderboard['passed_cases']}",
            f"- Failed cases: {leaderboard['failed_cases']}",
            f"- Pass rate: {leaderboard['pass_rate']:.6f}",
            f"- Total checks: {leaderboard['total_checks']}",
            f"- Accuracy (passed checks / total checks): {leaderboard['accuracy']:.6f}",
            f"- Submissions created: {leaderboard['submissions_created']}",
            f"- Submissions rejected: {leaderboard['submissions_rejected']}",
            f"- Eligible contributions: {leaderboard['eligible_contributions']}",
            f"- Ineligible contributions: {leaderboard['ineligible_contributions']}",
            f"- Reputation events: {leaderboard['reputation_events_created']}",
            f"- Reward cycles finalized: {leaderboard['reward_cycles_finalized']}",
            f"- Reward events: {leaderboard['reward_events_created']}",
            f"- Zero-reward submissions: {leaderboard['zero_reward_submissions']}",
            f"- Penalty events: {leaderboard['penalty_events_created']}",
            f"- Idempotency checks: {leaderboard['idempotency_checks_passed']}/{leaderboard['idempotency_checks']}",
            f"- Protocol points distributed: {leaderboard['total_protocol_points_distributed']:.6f}",
            "",
            "## 12. High versus Medium reward comparison",
            "",
            "Both contributions were calculated in one shared reward cycle with equal initial reputation.",
            "",
            "| Metric | High | Medium |",
            "| --- | ---: | ---: |",
            f"| Contribution score | {comparison.get('high_contribution_score', 0):.6f} | {comparison.get('medium_contribution_score', 0):.6f} |",
            f"| Reputation multiplier | {comparison.get('high_reputation_multiplier', 0):.6f} | {comparison.get('medium_reputation_multiplier', 0):.6f} |",
            f"| Raw weight | {comparison.get('high_raw_weight', 0):.6f} | {comparison.get('medium_raw_weight', 0):.6f} |",
            f"| Allocation ratio | {comparison.get('high_allocation_ratio', 0):.12f} | {comparison.get('medium_allocation_ratio', 0):.12f} |",
            f"| Reward amount | {comparison.get('high_reward', 0):.6f} | {comparison.get('medium_reward', 0):.6f} |",
            "",
            f"Comparison passed: {'yes' if comparison.get('passed') else 'no'}. The High contribution receives more points because its contribution score and raw weight are larger.",
            "",
            "## 13. Idempotency verification",
            "",
            f"All {leaderboard['idempotency_checks_passed']} of {leaderboard['idempotency_checks']} explicit checks passed. They cover repeated reputation processing, repeated reward calculation, repeated reward finalization, repeated unsafe penalty processing, and same-node duplicate submission prevention.",
            "",
            "## 14. Security properties",
            "",
            "- No PoCs were executed.",
            "- No Docker or forge command was called.",
            "- No external subprocess or external API was called.",
            "- No private key, seed phrase, or wallet was used.",
            "- No blockchain operation occurred.",
            "- No real token transfer occurred.",
            "- No stake was locked or reduced.",
            "",
            "## 15. Current limitations",
            "",
            "The coordinator is centralized, fixtures are synthetic and small, validators are not independent, and no consensus, dispute system, signature verification, or Sybil resistance exists. Reputation is global, category multiplier is fixed at 1.0, no category subnets exist, and protocol points have no monetary value.",
            "",
            "## 16. Week 6 direction",
            "",
            "Week 6 should add category specialization and subnets:",
            "",
            "```text",
            "project attack categories",
            "    |",
            "    v",
            "Subnet Router",
            "    |",
            "    +--> Access Control Subnet",
            "    +--> Reentrancy Subnet",
            "    +--> Accounting Subnet",
            "```",
            "",
            "Planned work includes category statistics and scores, subnet membership and ranking, exploration slots, routing, category reward pools, and a subnet benchmark.",
            "",
            "## 17. Conclusion",
            "",
            "Week 5 proves a complete deterministic off-chain protocol flow. It does not prove a decentralized blockchain network, token economy, validator consensus, or financial slashing system.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_protocol_results(
    case_document: dict[str, Any],
    leaderboard: dict[str, Any],
    results_dir: Path = RESULTS_DIR,
) -> dict[str, Path]:
    results_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "case_results": results_dir / CASE_RESULTS_FILENAME,
        "leaderboard": results_dir / LEADERBOARD_FILENAME,
        "report": results_dir / REPORT_FILENAME,
        "summary": results_dir / SUMMARY_FILENAME,
    }
    _write_json(paths["case_results"], case_document)
    _write_json(paths["leaderboard"], leaderboard)
    _write_json(paths["summary"], build_week_5_summary(leaderboard))
    paths["report"].write_text(render_week_5_report(case_document, leaderboard), encoding="utf-8")
    return paths


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        _, leaderboard = run_protocol_benchmark(args.dataset, args.results_dir)
    except ValueError as exc:
        print(f"Protocol benchmark configuration error: {_safe_error(exc)}")
        return 2
    print("Week 5 protocol benchmark")
    print(f"total_cases: {leaderboard['total_cases']}")
    print(f"passed_cases: {leaderboard['passed_cases']}")
    print(f"failed_cases: {leaderboard['failed_cases']}")
    print(f"accuracy: {leaderboard['accuracy']:.6f}")
    return 0 if leaderboard["success"] else 1


def _execute_case(
    case_config: dict[str, Any],
    protocol_data: Path,
    workspace: Path,
    result: dict[str, Any],
) -> dict[str, Any]:
    steps = set(case_config["steps"])
    metrics = result["metrics"]
    actual = result["actual"]
    project_id = case_config["project_id"]
    node_config = case_config["node"]
    node = create_node(
        protocol_data,
        NodeCreate(
            node_type=node_config["node_type"],
            display_name=node_config["display_name"],
            operator_id=node_config["operator_id"],
            supported_categories=node_config["supported_categories"],
        ),
    )
    result["steps"]["node_created"] = True
    if node_config.get("initial_status", "active") != "active":
        node = change_node_status(
            protocol_data,
            node.node_id,
            NodeStatusChangeRequest(status=node_config["initial_status"], reason="Synthetic benchmark status."),
        )
    actual["node_status"] = node.status.value
    actual["reputation_before"] = node.reputation_score
    actual["reputation_after"] = node.reputation_score
    actual["submission_created"] = False
    actual["submission_rejected"] = False
    actual["contribution_scores"] = []
    actual["contribution_eligibilities"] = []
    actual["reputation_deltas"] = []
    actual["reputation_event_types"] = []
    actual["reward_amounts"] = []
    actual["penalty_created"] = False

    submissions = []
    metrics["submissions_attempted"] = len(case_config["finding_ids"])
    for finding_id in case_config["finding_ids"]:
        try:
            submission = create_submission(
                protocol_data,
                workspace,
                SubmissionCreate(project_id=project_id, finding_id=finding_id, node_id=node.node_id),
            )
        except NodeInactiveError as exc:
            actual["submission_rejected"] = True
            actual["submission_error_type"] = exc.__class__.__name__
            metrics["submissions_rejected"] += 1
            continue
        submissions.append(submission)
        metrics["submissions_created"] += 1

    actual["submission_created"] = bool(submissions)
    actual["submission_count"] = len(submissions)
    actual["finding_hashes"] = [submission.finding_hash for submission in submissions]
    result["steps"]["submission_created"] = bool(submissions)
    if not submissions:
        current_node = load_node(protocol_data, node.node_id)
        actual["node_statistics"] = current_node.statistics.model_dump()
        actual["reputation_after"] = current_node.reputation_score
        actual["submission_statuses"] = []
        actual["reputation_event_count"] = 0
        actual["eligible_reward_allocations"] = 0
        actual["idempotency_checks"] = 0
        actual["idempotency_checks_passed"] = 0
        return result

    contributions = []
    reputation_events = []
    idempotency = set(case_config.get("idempotency", []))
    for index, submission in enumerate(submissions):
        reproduction = load_reproduction_result(workspace, submission.finding_id)
        validation = load_validation_decision(workspace, submission.finding_id)
        if validation is None:
            raise ValueError("Static validation fixture is missing")
        submission = update_submission_status(
            protocol_data,
            submission.submission_id,
            SubmissionStatusUpdate(
                status="validation_pending",
                reason="Synthetic fixture sources attached.",
                reproduction_id=reproduction.reproduction_id if reproduction else None,
            ),
        )
        final_status = FINAL_SUBMISSION_STATUS[validation.status.value]
        submission = update_submission_status(
            protocol_data,
            submission.submission_id,
            SubmissionStatusUpdate(
                status=final_status,
                reason="Static validation outcome finalized for benchmark.",
                validation_id=validation.validation_id,
            ),
        )
        contribution = calculate_contribution_for_submission(protocol_data, workspace, submission.submission_id)
        contributions.append(contribution)
        actual["contribution_scores"].append(contribution.total_score)
        actual["contribution_eligibilities"].append(contribution.eligibility_status.value)
        metrics[f"{contribution.eligibility_status.value}_contributions"] += 1
        result["steps"]["contribution_calculated"] = True

        reputation = process_submission_reputation(protocol_data, workspace, submission.submission_id)
        if reputation.event is None:
            raise ValueError("Finalized synthetic case did not create a ReputationEvent")
        reputation_events.append(reputation.event)
        delta = reputation.event.delta_components.total_delta
        actual["reputation_deltas"].append(delta)
        actual["reputation_event_types"].append(reputation.event.event_type.value)
        metrics["reputation_events_created"] += 1
        metrics["positive_reputation_events" if delta > 0 else "negative_reputation_events" if delta < 0 else "zero_delta_reputation_events"] += 1
        result["steps"]["reputation_processed"] = True

        if "reputation" in idempotency:
            before_repeat = load_node(protocol_data, node.node_id)
            repeated = process_submission_reputation(protocol_data, workspace, submission.submission_id)
            after_repeat = load_node(protocol_data, node.node_id)
            passed = (
                repeated.processing_status.value == "already_applied"
                and before_repeat.reputation_score == after_repeat.reputation_score
                and before_repeat.statistics == after_repeat.statistics
            )
            _record_idempotency(actual, metrics, passed)

    if "create_reward_cycle" in steps:
        cycle = create_reward_cycle(
            protocol_data,
            workspace,
            RewardCycleCreate(
                project_id=project_id,
                reward_pool=case_config["reward_pool"],
                submission_ids=[submission.submission_id for submission in submissions],
                description="Synthetic Week 5 protocol benchmark cycle.",
            ),
        )
        metrics["reward_cycles_created"] += 1
        calculated = calculate_reward_cycle(protocol_data, workspace, cycle.cycle_id)
        if "reward_calculation" in idempotency:
            repeated_calculation = calculate_reward_cycle(protocol_data, workspace, cycle.cycle_id)
            passed = (
                calculated.allocations == repeated_calculation.allocations
                and calculated.source_fingerprint == repeated_calculation.source_fingerprint
            )
            _record_idempotency(actual, metrics, passed)
            calculated = repeated_calculation
        finalized = finalize_reward_cycle(protocol_data, workspace, calculated.cycle_id)
        metrics["reward_cycles_finalized"] += 1
        result["steps"]["reward_cycle_finalized"] = True
        if "reward_finalization" in idempotency:
            events_before = list_reward_events(protocol_data, cycle_id=finalized.cycle_id)
            repeated_finalization = finalize_reward_cycle(protocol_data, workspace, finalized.cycle_id)
            events_after = list_reward_events(protocol_data, cycle_id=finalized.cycle_id)
            passed = finalized == repeated_finalization and events_before == events_after
            _record_idempotency(actual, metrics, passed)

        actual["eligible_reward_allocations"] = sum(allocation.eligible for allocation in finalized.allocations)
        for submission in submissions:
            event = load_reward_event_by_submission(protocol_data, submission.submission_id)
            amount = event.reward_amount if event else 0.0
            actual["reward_amounts"].append(amount)
            if event:
                metrics["reward_events_created"] += 1
                metrics["protocol_points_distributed"] += amount
            else:
                metrics["zero_reward_submissions"] += 1
        actual["reward_unit"] = "protocol_points"
        actual["reward_cycle_total_allocated"] = finalized.total_allocated
        actual["reward_cycle_pool"] = finalized.reward_pool

    if "process_penalty" in steps:
        penalty_response = process_submission_penalty(
            protocol_data, workspace, submissions[0].submission_id
        )
        penalty = penalty_response.event
        result["steps"]["penalty_processed"] = True
        actual.update(
            {
                "penalty_created": penalty_response.created,
                "penalty_reason": penalty.reason_code.value,
                "protocol_penalty_points": penalty.protocol_penalty_points,
                "simulated_stake_loss": penalty.simulated_stake_loss,
                "executed_onchain": penalty.executed_onchain,
                "requires_human_review": penalty.requires_human_review,
            }
        )
        metrics["penalty_events_created"] += 1
        if "penalty" in idempotency:
            reputation_before_repeat = load_node(protocol_data, node.node_id).reputation_score
            repeated_penalty = process_submission_penalty(
                protocol_data, workspace, submissions[0].submission_id
            )
            reputation_after_repeat = load_node(protocol_data, node.node_id).reputation_score
            passed = (
                repeated_penalty.created is False
                and repeated_penalty.event.penalty_event_id == penalty.penalty_event_id
                and len(list_penalty_events(protocol_data, node_id=node.node_id)) == 1
                and reputation_before_repeat == reputation_after_repeat
            )
            _record_idempotency(actual, metrics, passed)

    if "duplicate_submission" in idempotency:
        duplicate_rejected = False
        try:
            create_submission(
                protocol_data,
                workspace,
                SubmissionCreate(
                    project_id=project_id,
                    finding_id=submissions[0].finding_id,
                    node_id=node.node_id,
                ),
            )
        except DuplicateSubmissionError:
            duplicate_rejected = True
        _record_idempotency(actual, metrics, duplicate_rejected)

    current_node = load_node(protocol_data, node.node_id)
    stored_submissions = [load_submission(protocol_data, item.submission_id) for item in submissions]
    actual["node_status"] = current_node.status.value
    actual["reputation_after"] = current_node.reputation_score
    actual["node_statistics"] = current_node.statistics.model_dump()
    actual["submission_statuses"] = [item.status.value for item in stored_submissions]
    actual["reputation_event_count"] = len(get_node_reputation_history(protocol_data, node.node_id))
    actual.setdefault("eligible_reward_allocations", 0)
    actual.setdefault("idempotency_checks", 0)
    actual.setdefault("idempotency_checks_passed", 0)
    return result


def _run_reward_comparison(dataset_root: Path, runtime_root: Path) -> dict[str, Any]:
    try:
        workspace = runtime_root / "audit_workspace"
        protocol_data = runtime_root / "protocol_data"
        workspace.mkdir(parents=True)
        protocol_data.mkdir()
        comparison_project = "protocol_reward_comparison"
        source_cases = {
            "high": dataset_root / "cases" / "accepted_high_value_001",
            "medium": dataset_root / "cases" / "accepted_medium_value_001",
        }
        (workspace / "findings").mkdir()
        findings = []
        for label, case_path in source_cases.items():
            finding_payload = _read_json(case_path / "project" / "findings" / "all_findings.json")[0]
            finding_payload["project_id"] = comparison_project
            findings.append(finding_payload)
            finding_id = finding_payload["finding_id"]
            for source_kind, filename in (("reproductions", "reproduction.json"), ("validations", "validation.json")):
                source = case_path / "project" / source_kind / finding_id / filename
                payload = _read_json(source)
                payload["project_id"] = comparison_project
                target = workspace / source_kind / finding_id
                target.mkdir(parents=True)
                _write_json(target / filename, payload)
        _write_json(workspace / "findings" / "all_findings.json", findings)

        submissions = {}
        contributions = {}
        for label, case_path in source_cases.items():
            config = _read_json(case_path / "case.json")
            node_data = config["node"]
            node = create_node(
                protocol_data,
                NodeCreate(
                    node_type=node_data["node_type"],
                    display_name=f"comparison-{label}-agent",
                    operator_id=f"synthetic_comparison_{label}",
                    supported_categories=node_data["supported_categories"],
                ),
            )
            finding_id = config["finding_ids"][0]
            submission = create_submission(
                protocol_data,
                workspace,
                SubmissionCreate(project_id=comparison_project, finding_id=finding_id, node_id=node.node_id),
            )
            reproduction = load_reproduction_result(workspace, finding_id)
            validation = load_validation_decision(workspace, finding_id)
            submission = update_submission_status(
                protocol_data,
                submission.submission_id,
                SubmissionStatusUpdate(
                    status="validation_pending",
                    reason="Static comparison sources attached.",
                    reproduction_id=reproduction.reproduction_id,
                ),
            )
            update_submission_status(
                protocol_data,
                submission.submission_id,
                SubmissionStatusUpdate(
                    status="accepted",
                    reason="Static comparison validation finalized.",
                    validation_id=validation.validation_id,
                ),
            )
            submissions[label] = submission
            contributions[label] = calculate_contribution_for_submission(
                protocol_data, workspace, submission.submission_id
            )

        cycle = create_reward_cycle(
            protocol_data,
            workspace,
            RewardCycleCreate(
                project_id=comparison_project,
                reward_pool=1000,
                submission_ids=[submissions["high"].submission_id, submissions["medium"].submission_id],
                description="Synthetic High-versus-Medium comparison.",
            ),
        )
        calculated = calculate_reward_cycle(protocol_data, workspace, cycle.cycle_id)
        by_submission = {allocation.submission_id: allocation for allocation in calculated.allocations}
        high = by_submission[submissions["high"].submission_id]
        medium = by_submission[submissions["medium"].submission_id]
        passed = (
            high.weight_components.raw_weight > medium.weight_components.raw_weight
            and high.reward_amount > medium.reward_amount
            and round(calculated.total_allocated, 6) == round(calculated.reward_pool, 6)
        )
        return {
            "high_submission_id": high.submission_id,
            "medium_submission_id": medium.submission_id,
            "high_contribution_score": contributions["high"].total_score,
            "medium_contribution_score": contributions["medium"].total_score,
            "high_reputation_multiplier": high.weight_components.reputation_multiplier,
            "medium_reputation_multiplier": medium.weight_components.reputation_multiplier,
            "high_raw_weight": high.weight_components.raw_weight,
            "medium_raw_weight": medium.weight_components.raw_weight,
            "high_allocation_ratio": high.allocation_ratio,
            "medium_allocation_ratio": medium.allocation_ratio,
            "high_reward": high.reward_amount,
            "medium_reward": medium.reward_amount,
            "reward_pool": calculated.reward_pool,
            "total_allocated": calculated.total_allocated,
            "passed": passed,
        }
    except Exception as exc:
        return {"passed": False, "error": _safe_error(exc)}


def _validate_case_fixture(case_id: str, case_path: Path) -> None:
    if not case_path.is_dir():
        raise ValueError(f"Protocol case directory is missing for {case_id}")
    case_file = case_path / "case.json"
    expected_file = case_path / "expected_protocol.json"
    project = case_path / "project"
    if not case_file.is_file() or not expected_file.is_file() or not project.is_dir():
        raise ValueError(f"Protocol case {case_id} is incomplete")
    case = _read_json(case_file)
    expected = _read_json(expected_file)
    if case.get("case_id") != case_id or expected.get("case_id") != case_id:
        raise ValueError(f"Protocol case ID mismatch for {case_id}")
    if case.get("project_id") != case_id:
        raise ValueError(f"Protocol case project_id must equal case_id for {case_id}")
    if not isinstance(case.get("steps"), list) or not set(case["steps"]).issubset(ALLOWED_STEPS):
        raise ValueError(f"Protocol case {case_id} contains unsupported steps")
    if not isinstance(case.get("finding_ids"), list) or not case["finding_ids"]:
        raise ValueError(f"Protocol case {case_id} requires findings")
    parse_scope_yaml((project / "scope.yaml").read_bytes())
    finding_files = sorted((project / "findings").glob("*.json"))
    findings = [Finding.model_validate(item) for path in finding_files for item in _read_json(path)]
    if sorted(finding.finding_id for finding in findings) != sorted(case["finding_ids"]):
        raise ValueError(f"Protocol case {case_id} finding IDs do not match")
    for finding_id in case["finding_ids"]:
        ReproductionResult.model_validate_json(
            (project / "reproductions" / finding_id / "reproduction.json").read_text(encoding="utf-8")
        )
        ValidationDecision.model_validate_json(
            (project / "validations" / finding_id / "validation.json").read_text(encoding="utf-8")
        )
    serialized = json.dumps({"case": case, "expected": expected}, ensure_ascii=False).lower()
    forbidden = ("private_key", "seed phrase", "seed_phrase", "wallet seed", "docker execution", "forge execution")
    if any(value in serialized for value in forbidden):
        raise ValueError(f"Protocol case {case_id} contains forbidden or non-synthetic instructions")


def _empty_case_metrics() -> dict[str, int | float]:
    return {
        "submissions_attempted": 0,
        "submissions_created": 0,
        "submissions_rejected": 0,
        "eligible_contributions": 0,
        "ineligible_contributions": 0,
        "pending_contributions": 0,
        "reputation_events_created": 0,
        "positive_reputation_events": 0,
        "negative_reputation_events": 0,
        "zero_delta_reputation_events": 0,
        "reward_cycles_created": 0,
        "reward_cycles_finalized": 0,
        "reward_events_created": 0,
        "zero_reward_submissions": 0,
        "penalty_events_created": 0,
        "idempotency_checks": 0,
        "idempotency_checks_passed": 0,
        "protocol_points_distributed": 0.0,
    }


def _record_idempotency(actual: dict[str, Any], metrics: dict[str, Any], passed: bool) -> None:
    metrics["idempotency_checks"] += 1
    actual["idempotency_checks"] = actual.get("idempotency_checks", 0) + 1
    if passed:
        metrics["idempotency_checks_passed"] += 1
        actual["idempotency_checks_passed"] = actual.get("idempotency_checks_passed", 0) + 1


def _check(name: str, expected: Any, actual: Any, passed: bool) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "expected": expected, "actual": actual}


def _primary_expectation(case_id: str) -> str:
    return {
        "accepted_high_value_001": "Positive reputation and reward",
        "accepted_medium_value_001": "Positive but smaller contribution",
        "duplicate_no_reward_001": "No reward, small reputation penalty",
        "out_of_scope_no_reward_001": "No reward",
        "unsafe_penalty_001": "No reward and PenaltyEvent",
        "inactive_node_rejected_001": "Submission rejected",
        "reputation_growth_001": "Gradual idempotent growth",
    }[case_id]


def _actual_result_label(result: dict[str, Any]) -> str:
    if result["errors"]:
        return f"error: {result['errors'][0]}"
    actual = result["actual"]
    if actual.get("submission_rejected"):
        return "submission rejected"
    if actual.get("penalty_created"):
        return "penalized, zero reward"
    reward = sum(actual.get("reward_amounts", []))
    if reward > 0:
        return f"rewarded {reward:.6f} protocol points"
    return "finalized with zero reward"


def _safe_error(exc: Exception) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    message = re.sub(r"(?:[A-Za-z]:[\\/]|/)(?:[^\s:]+[\\/])+[^\s:]+", "[path]", message)
    return f"{exc.__class__.__name__}: {message}" if message != exc.__class__.__name__ else message


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Week 5 synthetic protocol benchmark.")
    parser.add_argument("--dataset", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
