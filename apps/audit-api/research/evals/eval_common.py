from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

APP_ROOT = Path(__file__).resolve().parents[2]
RESEARCH_ROOT = APP_ROOT / "research"
RESULTS_DIR = RESEARCH_ROOT / "results"
LEADERBOARD_PATH = RESULTS_DIR / "leaderboard.json"
CASE_RESULTS_PATH = RESULTS_DIR / "case_results.json"
REPORT_PATH = RESULTS_DIR / "week_2_5_report.md"

if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from app.agents.access_control_agent import AccessControlAgent
from app.agents.reentrancy_agent import ReentrancyAgent
from app.schemas.finding import FindingCreate
from app.schemas.scope import ScopeManifest


AGENTS = {
    "access_control": AccessControlAgent,
    "reentrancy": ReentrancyAgent,
}


def load_case(case_path: Path) -> dict[str, Any]:
    scope_path = case_path / "scope.yaml"
    scope_data = yaml.safe_load(scope_path.read_text(encoding="utf-8"))
    scope = ScopeManifest.model_validate(scope_data)
    return {
        "case_id": case_path.name,
        "case_path": case_path,
        "repo_path": case_path,
        "scope": scope.model_dump(),
    }


def load_expected_findings(case_path: Path) -> list[dict[str, Any]]:
    expected_path = case_path / "expected_findings.json"
    return json.loads(expected_path.read_text(encoding="utf-8"))


def run_agent_on_case(agent_name: str, case_path: Path) -> list[FindingCreate]:
    agent_cls = AGENTS.get(agent_name)
    if agent_cls is None:
        raise ValueError(f"Unknown agent: {agent_name}")

    case = load_case(case_path)
    agent = agent_cls()
    return agent.run(
        project_id=case["case_id"],
        repo_path=case["repo_path"],
        scope=case["scope"],
    )


def compare_findings(
    actual_findings: list[Any],
    expected_findings: list[dict[str, Any]],
    contracts_in_scope: list[str] | None = None,
) -> dict[str, Any]:
    matched_expected_indexes: set[int] = set()
    seen_actual: set[tuple[str, str, str]] = set()
    false_positive_keys: list[list[str]] = []

    metrics: dict[str, Any] = {
        "positive_case": len(expected_findings) > 0,
        "true_positives": 0,
        "false_positives": 0,
        "false_negatives": 0,
        "expected_findings": len(expected_findings),
        "actual_findings": len(actual_findings),
        "out_of_scope_findings": 0,
        "schema_valid_findings": 0,
        "schema_invalid_findings": 0,
        "duplicate_count": 0,
    }

    for actual in actual_findings:
        try:
            finding = _validate_actual_finding(actual)
        except ValidationError:
            metrics["schema_invalid_findings"] += 1
            metrics["false_positives"] += 1
            continue

        metrics["schema_valid_findings"] += 1

        key = _actual_key(finding)
        if key in seen_actual:
            metrics["duplicate_count"] += 1
        seen_actual.add(key)

        if contracts_in_scope is not None and _has_out_of_scope_contract(finding, contracts_in_scope):
            metrics["out_of_scope_findings"] += 1

        expected_index = _find_matching_expected_index(
            finding,
            expected_findings,
            matched_expected_indexes,
        )
        if expected_index is not None:
            metrics["true_positives"] += 1
            matched_expected_indexes.add(expected_index)
        else:
            metrics["false_positives"] += 1
            false_positive_keys.append(list(key))

    false_negative_keys = [
        _expected_key(expected)
        for index, expected in enumerate(expected_findings)
        if index not in matched_expected_indexes
    ]
    metrics["false_negatives"] = count_false_negatives(
        metrics["expected_findings"],
        metrics["true_positives"],
    )
    metrics["expected_count"] = metrics["expected_findings"]
    metrics["actual_count"] = metrics["actual_findings"]
    metrics["false_negative_count"] = metrics["false_negatives"]
    metrics["false_positive_keys"] = false_positive_keys
    metrics["false_negative_keys"] = [list(key) for key in false_negative_keys]
    metrics["precision"] = calculate_precision(metrics["true_positives"], metrics["false_positives"])
    metrics["recall"] = calculate_recall(
        metrics["true_positives"],
        metrics["expected_findings"],
        metrics["false_positives"],
    )
    metrics["f1_score"] = calculate_f1(metrics["precision"], metrics["recall"])
    metrics["score"] = calculate_score(metrics)
    return metrics


def count_positive_negative_cases(cases: list[dict[str, Any]]) -> tuple[int, int]:
    positive_cases = sum(1 for case in cases if case.get("positive_case", False))
    return positive_cases, len(cases) - positive_cases


def count_expected_findings(cases: list[dict[str, Any]]) -> int:
    return sum(int(case.get("expected_findings", 0)) for case in cases)


def count_actual_findings(actual_findings: list[Any]) -> int:
    return len(actual_findings)


def count_false_negatives(expected_count: int, true_positives: int) -> int:
    return max(0, expected_count - true_positives)


def calculate_precision(true_positives: int, false_positives: int) -> float:
    denominator = true_positives + false_positives
    if denominator == 0:
        return 0.0
    return true_positives / denominator


def calculate_recall(true_positives: int, expected_findings: int, false_positives: int = 0) -> float:
    if expected_findings == 0:
        return 1.0 if false_positives == 0 else 0.0
    return true_positives / expected_findings


def calculate_f1(precision: float, recall: float) -> float:
    denominator = precision + recall
    if denominator == 0:
        return 0.0
    return 2 * precision * recall / denominator


def calculate_score(metrics: dict[str, Any]) -> int:
    return (
        5 * int(metrics.get("true_positives", 0))
        - 4 * int(metrics.get("false_positives", 0))
        - 3 * int(metrics.get("false_negatives", metrics.get("false_negative_count", 0)))
        - 2 * int(metrics.get("out_of_scope_findings", 0))
        - int(metrics.get("duplicate_count", 0))
        - 2 * int(metrics.get("schema_invalid_findings", 0))
    )


def aggregate_agent_metrics(case_results: list[dict[str, Any]]) -> dict[str, Any]:
    positive_cases, negative_cases = count_positive_negative_cases(case_results)
    true_positives = sum(int(case["true_positives"]) for case in case_results)
    false_positives = sum(int(case["false_positives"]) for case in case_results)
    expected_findings = count_expected_findings(case_results)

    summary = {
        "total_cases": len(case_results),
        "positive_cases": positive_cases,
        "negative_cases": negative_cases,
        "expected_findings": expected_findings,
        "actual_findings": sum(int(case["actual_findings"]) for case in case_results),
        "true_positives": true_positives,
        "false_positives": false_positives,
        "false_negatives": count_false_negatives(expected_findings, true_positives),
        "out_of_scope_findings": sum(int(case["out_of_scope_findings"]) for case in case_results),
        "duplicate_count": sum(int(case["duplicate_count"]) for case in case_results),
        "schema_invalid_findings": sum(int(case["schema_invalid_findings"]) for case in case_results),
    }
    summary["precision"] = calculate_precision(summary["true_positives"], summary["false_positives"])
    summary["recall"] = calculate_recall(
        summary["true_positives"],
        summary["expected_findings"],
        summary["false_positives"],
    )
    summary["f1_score"] = calculate_f1(summary["precision"], summary["recall"])
    summary["score"] = calculate_score(summary)
    return summary


def write_leaderboard(results: dict[str, Any], output_path: Path | None = None) -> Path:
    output_path = output_path or LEADERBOARD_PATH
    output_path.parent.mkdir(parents=True, exist_ok=True)
    leaderboard = _read_json(output_path, default={})
    leaderboard.update(
        {
            agent_name: _leaderboard_summary(summary)
            for agent_name, summary in results.items()
        }
    )
    output_path.write_text(
        json.dumps(leaderboard, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return output_path


def write_case_results(results: dict[str, list[dict[str, Any]]], output_path: Path | None = None) -> Path:
    output_path = output_path or CASE_RESULTS_PATH
    output_path.parent.mkdir(parents=True, exist_ok=True)
    stored_results = _read_json(output_path, default={})
    stored_results.update(results)
    output_path.write_text(
        json.dumps(stored_results, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return output_path


def evaluate_agent(agent_name: str, cases_dir: Path) -> dict[str, Any]:
    case_results = evaluate_agent_cases(agent_name, cases_dir)
    return {agent_name: aggregate_agent_metrics(case_results)}


def evaluate_agent_cases(agent_name: str, cases_dir: Path) -> list[dict[str, Any]]:
    case_results = []
    for case_path in sorted(path for path in cases_dir.iterdir() if path.is_dir()):
        case = load_case(case_path)
        expected = load_expected_findings(case_path)
        actual = run_agent_on_case(agent_name, case_path)
        metrics = compare_findings(
            actual,
            expected,
            contracts_in_scope=case["scope"]["contracts_in_scope"],
        )
        metrics["case_id"] = case["case_id"]
        case_results.append(_case_result_summary(metrics))

    return case_results


def summarize_results(agent_name: str, case_metrics: list[dict[str, Any]]) -> dict[str, Any]:
    return {agent_name: aggregate_agent_metrics(case_metrics)}


def print_summary(agent_name: str, results: dict[str, Any]) -> None:
    summary = results[agent_name]
    title = " ".join(part.capitalize() for part in agent_name.split("_"))
    print(f"{title} Evaluation")
    print(f"Total cases: {summary['total_cases']}")
    print(f"Positive cases: {summary['positive_cases']}")
    print(f"Negative cases: {summary['negative_cases']}")
    print(f"Expected findings: {summary['expected_findings']}")
    print(f"Actual findings: {summary['actual_findings']}")
    print(f"True positives: {summary['true_positives']}")
    print(f"False positives: {summary['false_positives']}")
    print(f"False negatives: {summary['false_negatives']}")
    print(f"Out-of-scope findings: {summary['out_of_scope_findings']}")
    print(f"Duplicate findings: {summary['duplicate_count']}")
    print(f"Schema invalid findings: {summary['schema_invalid_findings']}")
    print(f"Precision: {summary['precision']:.2f}")
    print(f"Recall: {summary['recall']:.2f}")
    print(f"F1 score: {summary['f1_score']:.2f}")
    print(f"Score: {summary['score']}")


def write_week_2_5_report() -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    leaderboard = _read_json(LEADERBOARD_PATH, default={})
    case_results = _read_json(CASE_RESULTS_PATH, default={})
    lines = [
        "# Week 2.5 Research Evaluation Report",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "Findings produced by agents are candidate/unverified only. This benchmark does not validate findings or execute exploits.",
        "",
        "## Leaderboard Summary",
    ]

    if not leaderboard:
        lines.append("- None yet")
    else:
        lines.extend(
            [
                "| Agent | Cases | TP | FP | FN | Precision | Recall | F1 | Score |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for agent_name in sorted(leaderboard):
            summary = leaderboard[agent_name]
            lines.append(
                "| "
                f"{agent_name} | "
                f"{summary.get('total_cases', 0)} | "
                f"{summary.get('true_positives', 0)} | "
                f"{summary.get('false_positives', 0)} | "
                f"{summary.get('false_negatives', 0)} | "
                f"{summary.get('precision', 0.0):.2f} | "
                f"{summary.get('recall', 0.0):.2f} | "
                f"{summary.get('f1_score', 0.0):.2f} | "
                f"{summary.get('score', 0)} |"
            )

    lines.extend(["", "## Metrics Per Agent"])
    for agent_name in sorted(leaderboard):
        summary = leaderboard[agent_name]
        lines.extend(
            [
                f"### {agent_name}",
                "",
                f"- Total cases: {summary.get('total_cases', 0)}",
                f"- Positive cases: {summary.get('positive_cases', 0)}",
                f"- Negative cases: {summary.get('negative_cases', 0)}",
                f"- Expected findings: {summary.get('expected_findings', 0)}",
                f"- Actual findings: {summary.get('actual_findings', 0)}",
                f"- True positives: {summary.get('true_positives', 0)}",
                f"- False positives: {summary.get('false_positives', 0)}",
                f"- False negatives: {summary.get('false_negatives', 0)}",
                f"- Out-of-scope findings: {summary.get('out_of_scope_findings', 0)}",
                f"- Duplicate findings: {summary.get('duplicate_count', 0)}",
                f"- Schema invalid findings: {summary.get('schema_invalid_findings', 0)}",
                f"- Precision: {summary.get('precision', 0.0):.2f}",
                f"- Recall: {summary.get('recall', 0.0):.2f}",
                f"- F1 score: {summary.get('f1_score', 0.0):.2f}",
                f"- Score: {summary.get('score', 0)}",
                "",
            ]
        )

    lines.extend(["## Per-Case Summary"])
    if not case_results:
        lines.append("- None yet")
    else:
        for agent_name in sorted(case_results):
            lines.extend(["", f"### {agent_name}", ""])
            lines.extend(
                [
                    "| Case | Positive | Expected | Actual | TP | FP | FN | Precision | Recall | F1 | Score |",
                    "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
                ]
            )
            for case in case_results[agent_name]:
                lines.append(
                    "| "
                    f"{case['case_id']} | "
                    f"{case['positive_case']} | "
                    f"{case['expected_findings']} | "
                    f"{case['actual_findings']} | "
                    f"{case['true_positives']} | "
                    f"{case['false_positives']} | "
                    f"{case['false_negatives']} | "
                    f"{case['precision']:.2f} | "
                    f"{case['recall']:.2f} | "
                    f"{case['f1_score']:.2f} | "
                    f"{case['score']} |"
                )

    lines.extend(
        [
            "",
            "## Best Performing Agent",
            f"- {_best_performing_agent(leaderboard)}",
            "",
            "## Weakest Metric Per Agent",
        ]
    )
    if not leaderboard:
        lines.append("- None yet")
    else:
        for agent_name in sorted(leaderboard):
            lines.append(f"- {agent_name}: {_weakest_metric(leaderboard[agent_name])}")

    lines.extend(
        [
            "",
            "## Notes",
            "- Findings are still candidate/unverified.",
            "- No PoC validation is implemented yet.",
            "- The benchmark is synthetic and small.",
            "",
            "## Recommended Next Improvements",
            "- Expand benchmark cases for modifiers, role-based access control, and inherited contracts.",
            "- Improve Solidity parsing before relying on more complex project layouts.",
            "- Track false positive and false negative categories per rule change.",
            "- Keep findings as candidate/unverified until a validator or reproduction layer exists.",
        ]
    )

    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return REPORT_PATH


def _validate_actual_finding(actual: Any) -> dict[str, Any]:
    if isinstance(actual, FindingCreate):
        finding = actual
    else:
        finding = FindingCreate.model_validate(actual)
    return finding.model_dump(mode="json")


def _expected_key(expected: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(expected["category"]),
        str(expected["contract"]),
        str(expected.get("function", "")),
    )


def _actual_key(actual: dict[str, Any]) -> tuple[str, str, str]:
    contract = actual.get("contracts", [""])[0] if actual.get("contracts") else ""
    function = actual.get("functions", [""])[0] if actual.get("functions") else ""
    return (str(actual["category"]), str(contract), str(function))


def _has_out_of_scope_contract(actual: dict[str, Any], contracts_in_scope: list[str]) -> bool:
    return any(contract not in contracts_in_scope for contract in actual.get("contracts", []))


def _find_matching_expected_index(
    actual: dict[str, Any],
    expected_findings: list[dict[str, Any]],
    matched_expected_indexes: set[int],
) -> int | None:
    for index, expected in enumerate(expected_findings):
        if index in matched_expected_indexes:
            continue
        if _finding_matches_expected(actual, expected):
            return index
    return None


def _finding_matches_expected(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    actual_contracts = actual.get("contracts", [])
    actual_functions = actual.get("functions", [])
    expected_function = expected.get("function")

    if actual.get("category") != expected.get("category"):
        return False
    if expected.get("contract") not in actual_contracts:
        return False
    if expected_function and expected_function not in actual_functions:
        return False
    if expected.get("root_cause_keywords") and not _has_root_cause_keyword_overlap(
        actual.get("root_cause", ""),
        expected["root_cause_keywords"],
    ):
        return False
    return True


def _has_root_cause_keyword_overlap(root_cause: str, keywords: list[str]) -> bool:
    root_cause_tokens = _tokenize(root_cause)
    for keyword in keywords:
        keyword_lower = keyword.lower()
        if keyword_lower in root_cause.lower():
            return True
        if root_cause_tokens.intersection(_tokenize(keyword_lower)):
            return True
    return False


def _tokenize(value: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", value.lower()) if len(token) > 3}


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _leaderboard_summary(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "total_cases": int(summary.get("total_cases", 0)),
        "positive_cases": int(summary.get("positive_cases", 0)),
        "negative_cases": int(summary.get("negative_cases", 0)),
        "expected_findings": int(summary.get("expected_findings", 0)),
        "actual_findings": int(summary.get("actual_findings", 0)),
        "true_positives": int(summary.get("true_positives", 0)),
        "false_positives": int(summary.get("false_positives", 0)),
        "false_negatives": int(summary.get("false_negatives", 0)),
        "out_of_scope_findings": int(summary.get("out_of_scope_findings", 0)),
        "duplicate_count": int(summary.get("duplicate_count", 0)),
        "schema_invalid_findings": int(summary.get("schema_invalid_findings", 0)),
        "precision": float(summary.get("precision", 0.0)),
        "recall": float(summary.get("recall", 0.0)),
        "f1_score": float(summary.get("f1_score", 0.0)),
        "score": int(summary.get("score", 0)),
    }

def _case_result_summary(metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": metrics["case_id"],
        "positive_case": bool(metrics["positive_case"]),
        "expected_findings": int(metrics["expected_findings"]),
        "actual_findings": int(metrics["actual_findings"]),
        "true_positives": int(metrics["true_positives"]),
        "false_positives": int(metrics["false_positives"]),
        "false_negatives": int(metrics["false_negatives"]),
        "out_of_scope_findings": int(metrics["out_of_scope_findings"]),
        "duplicate_count": int(metrics["duplicate_count"]),
        "schema_invalid_findings": int(metrics["schema_invalid_findings"]),
        "precision": float(metrics["precision"]),
        "recall": float(metrics["recall"]),
        "f1_score": float(metrics["f1_score"]),
        "score": int(metrics["score"]),
    }


def _best_performing_agent(leaderboard: dict[str, Any]) -> str:
    if not leaderboard:
        return "None yet"
    agent_name, summary = max(
        leaderboard.items(),
        key=lambda item: (item[1].get("score", 0), item[1].get("f1_score", 0.0)),
    )
    return f"{agent_name} with score {summary.get('score', 0)} and F1 {summary.get('f1_score', 0.0):.2f}"


def _weakest_metric(summary: dict[str, Any]) -> str:
    metric_values = {
        "precision": float(summary.get("precision", 0.0)),
        "recall": float(summary.get("recall", 0.0)),
        "f1_score": float(summary.get("f1_score", 0.0)),
    }
    metric, value = min(metric_values.items(), key=lambda item: item[1])
    return f"{metric} ({value:.2f})"
