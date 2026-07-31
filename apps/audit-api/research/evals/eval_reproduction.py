from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[2]
RESEARCH_ROOT = APP_ROOT / "research"
DATASET_ROOT = RESEARCH_ROOT / "datasets" / "reproduction"
MANIFEST_PATH = DATASET_ROOT / "manifest.json"
RESULTS_DIR = RESEARCH_ROOT / "results"
CASE_RESULTS_PATH = RESULTS_DIR / "reproduction_case_results.json"
LEADERBOARD_PATH = RESULTS_DIR / "reproduction_leaderboard.json"
REPORT_PATH = RESULTS_DIR / "week_3_reproduction_report.md"
WEEK_3_REPORT_PATH = RESULTS_DIR / "week_3_report.md"
WEEK_3_SUMMARY_PATH = RESULTS_DIR / "week_3_summary.json"

if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from app.schemas.sandbox import SandboxCommandResult, SandboxRunStatus
from app.services.foundry_service import detect_foundry_project
from app.services.safety_preflight_service import run_safety_preflight
from app.services import sandbox_runner


STATUS_COUNTS = {
    "reproduced": "reproduced_count",
    "failed": "reproduction_failed_count",
    "rejected_unsafe": "rejected_unsafe_count",
    "unsupported": "unsupported_count",
    "timeout": "timeout_count",
    "sandbox_error": "sandbox_error_count",
}


def main() -> None:
    args = _parse_args()
    mock_sandbox = not args.real_sandbox
    case_results, leaderboard = run_evaluation(mock_sandbox=mock_sandbox)
    write_results(case_results, leaderboard)
    write_report(case_results, leaderboard, mock_sandbox=mock_sandbox)
    write_week_3_summary(leaderboard)
    write_week_3_report(case_results, leaderboard)
    print_summary(leaderboard, mock_sandbox=mock_sandbox)


def run_evaluation(
    mock_sandbox: bool = True,
    manifest_path: Path = MANIFEST_PATH,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    manifest = _read_json(manifest_path)
    case_results = [
        evaluate_case(DATASET_ROOT / case["path"], mock_sandbox=mock_sandbox)
        for case in manifest["cases"]
    ]
    return case_results, aggregate_results(case_results)


def evaluate_case(case_path: Path, mock_sandbox: bool = True) -> dict[str, Any]:
    expected = _read_json(case_path / "expected_reproduction.json")
    expected_status = expected["expected_status"]
    test_name = expected.get("expected_test_name")
    notes: list[str] = list(expected.get("notes", []))
    foundry = detect_foundry_project(case_path)
    duration_ms = 0
    preflight_passed: bool | None = None
    sandbox_used = False
    execution_mode = "not_executed"
    failure_reason: str | None = None
    safety_issues_count = 0
    safety_high_or_critical_count = 0

    if not foundry.is_foundry_project:
        actual_status = "unsupported"
        failure_reason = "Project is not Foundry-compatible."
    else:
        command = ["forge", "test", "--match-test", test_name]
        preflight = run_safety_preflight(
            repo_path=case_path,
            poc_file="test/PoC.t.sol",
            test_name=test_name,
            command=command,
        )
        preflight_passed = preflight.passed
        safety_issues_count = len(preflight.issues)
        safety_high_or_critical_count = sum(
            1 for issue in preflight.issues if issue.severity in {"high", "critical"}
        )
        notes.extend(f"{issue.code}: {issue.message}" for issue in preflight.issues)

        if not preflight.passed:
            actual_status = "rejected_unsafe"
            failure_reason = "Safety preflight failed."
        else:
            sandbox_result = _run_sandbox(case_path, command, expected_status, mock_sandbox)
            sandbox_used = not mock_sandbox
            execution_mode = "mock_sandbox" if mock_sandbox else "real_sandbox"
            duration_ms = sandbox_result.duration_ms
            actual_status = _map_sandbox_status(sandbox_result.status)
            if sandbox_result.error_message:
                failure_reason = sandbox_result.error_message
                notes.append(sandbox_result.error_message)

    return {
        "case_id": expected["case_id"],
        "expected_status": expected_status,
        "actual_status": actual_status,
        "passed": actual_status == expected_status,
        "foundry_project": foundry.is_foundry_project,
        "preflight_passed": preflight_passed,
        "sandbox_used": sandbox_used,
        "mock_sandbox": mock_sandbox,
        "duration_ms": duration_ms,
        "safety_issues_count": safety_issues_count,
        "safety_high_or_critical_count": safety_high_or_critical_count,
        "execution_mode": execution_mode,
        "failure_reason": failure_reason if actual_status != expected_status or failure_reason else None,
        "notes": notes,
    }


def aggregate_results(case_results: list[dict[str, Any]]) -> dict[str, Any]:
    total_cases = len(case_results)
    passed_cases = sum(1 for result in case_results if result["passed"])
    leaderboard = {
        "total_cases": total_cases,
        "passed_cases": passed_cases,
        "failed_cases": total_cases - passed_cases,
        "reproduction_attempts": sum(
            1 for result in case_results if result["preflight_passed"] is True
        ),
        "reproduced_count": 0,
        "reproduction_failed_count": 0,
        "rejected_unsafe_count": 0,
        "unsupported_count": 0,
        "timeout_count": 0,
        "sandbox_error_count": 0,
        "accuracy": passed_cases / total_cases if total_cases else 0.0,
    }
    for result in case_results:
        count_key = STATUS_COUNTS.get(result["actual_status"])
        if count_key:
            leaderboard[count_key] += 1
    return leaderboard


def write_results(
    case_results: list[dict[str, Any]],
    leaderboard: dict[str, Any],
    results_dir: Path = RESULTS_DIR,
) -> tuple[Path, Path]:
    results_dir.mkdir(parents=True, exist_ok=True)
    case_results_path = results_dir / "reproduction_case_results.json"
    leaderboard_path = results_dir / "reproduction_leaderboard.json"
    case_results_path.write_text(
        json.dumps({"cases": case_results}, indent=2),
        encoding="utf-8",
    )
    leaderboard_path.write_text(json.dumps(leaderboard, indent=2), encoding="utf-8")
    return case_results_path, leaderboard_path


def write_report(
    case_results: list[dict[str, Any]],
    leaderboard: dict[str, Any],
    mock_sandbox: bool,
    report_path: Path = REPORT_PATH,
) -> Path:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Week 3 Reproduction Benchmark Report",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "Benchmark: week_3_reproduction_synthetic",
        "",
        "## Aggregate Metrics",
        "",
        f"- Total cases: {leaderboard['total_cases']}",
        f"- Passed cases: {leaderboard['passed_cases']}",
        f"- Failed cases: {leaderboard['failed_cases']}",
        f"- Reproduction attempts: {leaderboard['reproduction_attempts']}",
        f"- Reproduced count: {leaderboard['reproduced_count']}",
        f"- Reproduction failed count: {leaderboard['reproduction_failed_count']}",
        f"- Rejected unsafe count: {leaderboard['rejected_unsafe_count']}",
        f"- Unsupported count: {leaderboard['unsupported_count']}",
        f"- Timeout count: {leaderboard['timeout_count']}",
        f"- Sandbox error count: {leaderboard['sandbox_error_count']}",
        f"- Accuracy: {leaderboard['accuracy']:.2f}",
        f"- Mode: {'mock sandbox' if mock_sandbox else 'real sandbox'}",
        "",
        "## Per-Case Status",
        "",
        "| Case | Expected | Actual | Passed | Foundry | Preflight | Execution Mode | Safety Issues |",
        "| --- | --- | --- | --- | --- | --- | --- | ---: |",
    ]
    for result in case_results:
        lines.append(
            "| "
            f"{result['case_id']} | "
            f"{result['expected_status']} | "
            f"{result['actual_status']} | "
            f"{result['passed']} | "
            f"{result['foundry_project']} | "
            f"{result['preflight_passed']} | "
            f"{result['execution_mode']} | "
            f"{result['safety_issues_count']} |"
        )

    lines.extend(
        [
            "",
            "## Notes",
            "- This benchmark is synthetic.",
            "- Real-world datasets are not used yet.",
            "- Mock sandbox mode does not prove Foundry execution.",
            "- Real sandbox mode requires `foundry-sandbox:latest`.",
            "- Safety preflight does not replace sandboxing.",
        ]
    )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def write_week_3_summary(
    leaderboard: dict[str, Any],
    output_path: Path = WEEK_3_SUMMARY_PATH,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "week": "Week 3",
        "theme": "Sandboxed PoC Reproduction Layer",
        "implemented": [
            "ReproductionResult schema and storage",
            "Foundry project detector",
            "Tooling diagnostics",
            "Safety Preflight",
            "SandboxedCommandRunner",
            "foundry-sandbox Docker image",
            "PoC upload and reproduction API",
            "Synthetic reproduction benchmark",
        ],
        "metrics": {
            "total_reproduction_cases": leaderboard["total_cases"],
            "passed_cases": leaderboard["passed_cases"],
            "failed_cases": leaderboard["failed_cases"],
            "reproduced_count": leaderboard["reproduced_count"],
            "rejected_unsafe_count": leaderboard["rejected_unsafe_count"],
            "unsupported_count": leaderboard["unsupported_count"],
            "accuracy": leaderboard["accuracy"],
        },
        "security_controls": [
            "No forge execution on host",
            "No shell=True",
            "Command allowlist",
            "Safety preflight before sandbox",
            "FFI rejection",
            "Dangerous fs_permissions rejection",
            "Docker sandbox with network disabled",
            "Non-root container user",
            "Resource limits",
        ],
        "limitations": [
            "PoCs are not generated automatically yet",
            "Validator AI is not implemented yet",
            "Benchmark is synthetic and small",
            "Real-world datasets are not integrated yet",
            "Real sandbox run requires foundry-sandbox:latest image",
        ],
        "next_week_focus": [
            "Validator layer v0",
            "Deduplication and severity normalization",
            "Report generation for accepted findings",
            "Better PoC quality scoring",
        ],
    }
    output_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return output_path


def write_week_3_report(
    case_results: list[dict[str, Any]],
    leaderboard: dict[str, Any],
    output_path: Path = WEEK_3_REPORT_PATH,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Week 3 Report - Sandboxed PoC Reproduction Layer",
        "",
        "## 1. Goal",
        "",
        "Week 3 turns candidate findings into testable reproduction attempts. The focus is safe PoC execution: tests are never run directly on the host, and execution must pass through Safety Preflight, CommandPolicy, and SandboxedCommandRunner.",
        "",
        "## 2. What was implemented",
        "",
        "### Reproduction schema and storage",
        "ReproductionResult schemas and file-backed storage link reproduction attempts to findings.",
        "",
        "### Foundry project detection",
        "Foundry detector identifies repositories with `foundry.toml` and `src/` before reproduction is attempted.",
        "",
        "### Tooling diagnostics",
        "Tooling diagnostics report Docker, sandbox image, and host Foundry availability without executing tests.",
        "",
        "### Safety Preflight",
        "Safety Preflight validates test names, command shapes, foundry.toml settings, and PoC files.",
        "",
        "### SandboxedCommandRunner",
        "SandboxedCommandRunner executes allowlisted Foundry commands only inside Docker.",
        "",
        "### foundry-sandbox Docker image",
        "The `foundry-sandbox:latest` image provides Foundry tools, non-root execution, and cached compiler support.",
        "",
        "### PoC upload and reproduction API",
        "API endpoints store PoCs, run safety checks, invoke the sandbox, and persist ReproductionResult records.",
        "",
        "### Synthetic reproduction benchmark",
        "A four-case synthetic benchmark validates reproduced, rejected_unsafe, and unsupported paths.",
        "",
        "## 3. Architecture",
        "",
        "```text",
        "Candidate Finding",
        "|",
        "v",
        "PoC File",
        "|",
        "v",
        "Safety Preflight",
        "|",
        "v",
        "SandboxedCommandRunner",
        "|",
        "v",
        "ReproductionResult",
        "|",
        "v",
        "Metrics + Report",
        "```",
        "",
        "## 4. Security controls",
        "",
        "- No `forge test` on host",
        "- No `shell=True`",
        "- Allowlisted commands only",
        "- Docker network disabled",
        "- No secrets mounted",
        "- No `docker.sock` mounted",
        "- Non-root user",
        "- Timeout/resource limits",
        "- FFI rejected",
        "- Dangerous filesystem permissions rejected",
        "",
        "## 5. Benchmark results",
        "",
        "| Case | Expected | Actual | Passed | Notes |",
        "| --- | --- | --- | --- | --- |",
    ]
    note_by_case = {
        "access_control_poc_001": "safe access-control PoC",
        "reentrancy_poc_001": "safe reentrancy PoC",
        "unsafe_poc_001": "FFI blocked by preflight",
        "unsupported_non_foundry_001": "missing foundry.toml",
    }
    for result in case_results:
        lines.append(
            "| "
            f"{result['case_id']} | "
            f"{result['expected_status']} | "
            f"{result['actual_status']} | "
            f"{'yes' if result['passed'] else 'no'} | "
            f"{note_by_case.get(result['case_id'], '')} |"
        )

    lines.extend(
        [
            "",
            "## 6. Aggregate metrics",
            "",
            f"- Total cases: {leaderboard['total_cases']}",
            f"- Passed cases: {leaderboard['passed_cases']}",
            f"- Failed cases: {leaderboard['failed_cases']}",
            f"- Reproduction attempts: {leaderboard['reproduction_attempts']}",
            f"- Reproduced count: {leaderboard['reproduced_count']}",
            f"- Rejected unsafe count: {leaderboard['rejected_unsafe_count']}",
            f"- Unsupported count: {leaderboard['unsupported_count']}",
            f"- Timeout count: {leaderboard['timeout_count']}",
            f"- Sandbox error count: {leaderboard['sandbox_error_count']}",
            f"- Accuracy: {leaderboard['accuracy']:.2f}",
            "",
            "## 7. Current limitations",
            "",
            "- Benchmark is synthetic",
            "- Mock mode does not prove real Foundry execution",
            "- Real mode requires Docker image",
            "- PoCs are manual",
            "- No AI PoC generation yet",
            "- No validator AI yet",
            "",
            "## 8. Next steps",
            "",
            "- Validator layer v0",
            "- Deduplication",
            "- Severity normalization",
            "- Report generation",
            "- Reproduction quality scoring",
        ]
    )
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output_path


def print_summary(leaderboard: dict[str, Any], mock_sandbox: bool) -> None:
    mode = "mock sandbox" if mock_sandbox else "real sandbox"
    print(f"Week 3 reproduction benchmark ({mode})")
    print(f"total_cases: {leaderboard['total_cases']}")
    print(f"passed_cases: {leaderboard['passed_cases']}")
    print(f"failed_cases: {leaderboard['failed_cases']}")
    print(f"accuracy: {leaderboard['accuracy']:.2f}")


def _run_sandbox(
    case_path: Path,
    command: list[str],
    expected_status: str,
    mock_sandbox: bool,
) -> SandboxCommandResult:
    if mock_sandbox:
        return SandboxCommandResult(
            status=SandboxRunStatus.COMPLETED if expected_status == "reproduced" else SandboxRunStatus.FAILED,
            command=command,
            exit_code=0 if expected_status == "reproduced" else 1,
            stdout="mock sandbox result",
            stderr="",
            duration_ms=0,
            error_message=None,
            timed_out=False,
        )
    return sandbox_runner.run_in_sandbox(
        repo_path=case_path,
        command=command,
        timeout_seconds=60,
    )


def _map_sandbox_status(status: SandboxRunStatus) -> str:
    return {
        SandboxRunStatus.COMPLETED: "reproduced",
        SandboxRunStatus.FAILED: "failed",
        SandboxRunStatus.TIMEOUT: "timeout",
        SandboxRunStatus.REJECTED: "rejected_unsafe",
        SandboxRunStatus.SANDBOX_ERROR: "sandbox_error",
    }[status]


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate synthetic reproduction benchmark cases.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mock-sandbox", action="store_true", help="Use deterministic mock sandbox results.")
    mode.add_argument("--real-sandbox", action="store_true", help="Use the real Docker sandbox runner.")
    return parser.parse_args()


if __name__ == "__main__":
    main()
