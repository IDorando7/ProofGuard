from __future__ import annotations

import json

import pytest

from research.benchmarks.week6_subnet_benchmark import run_week6_subnet_benchmark
from research.reports.week6_subnet_report import (
    DEFERRED_REFINEMENTS,
    REQUIRED_REPORT_SECTIONS,
)
from research.schemas.week6_subnet_benchmark import (
    GENERATED_FILENAMES,
    REQUIRED_CASE_IDS,
    Week6BenchmarkSummary,
    Week6CaseResultsDocument,
)


@pytest.fixture(scope="module")
def outputs(tmp_path_factory):
    output_dir = tmp_path_factory.mktemp("week6-reports")
    cases, leaderboard, summary = run_week6_subnet_benchmark(output_dir)
    return output_dir, cases, leaderboard, summary


def _json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_all_required_outputs_exist_and_validate(outputs):
    output_dir, _, _, _ = outputs
    assert {path.name for path in output_dir.iterdir()} == set(GENERATED_FILENAMES)
    Week6CaseResultsDocument.model_validate(
        _json(output_dir / "subnet_case_results.json")
    )
    Week6BenchmarkSummary.model_validate(_json(output_dir / "week_6_summary.json"))


def test_case_order_counts_and_summary_are_consistent(outputs):
    output_dir, _, _, _ = outputs
    cases = _json(output_dir / "subnet_case_results.json")
    summary = _json(output_dir / "week_6_summary.json")
    assert tuple(item["case_id"] for item in cases["cases"]) == REQUIRED_CASE_IDS
    assertions = [
        assertion for case in cases["cases"] for assertion in case["assertions"]
    ]
    assert summary["total_cases"] == len(cases["cases"]) == 7
    assert summary["passed_cases"] == sum(case["passed"] for case in cases["cases"])
    assert summary["total_assertions"] == len(assertions)
    assert summary["passed_assertions"] == sum(item["passed"] for item in assertions)
    assert summary["deterministic_replay_passed"] is True


def test_leaderboard_categories_order_and_positions_are_report_only(outputs):
    output_dir, _, _, _ = outputs
    leaderboard = _json(output_dir / "subnet_leaderboard.json")
    assert [item["category"] for item in leaderboard["categories"]] == [
        "access_control",
        "reentrancy",
    ]
    assert leaderboard["report_only_position"] is True
    for category in leaderboard["categories"]:
        assert [item["position"] for item in category["nodes"]] == list(
            range(1, len(category["nodes"]) + 1)
        )
        assert all("rank" not in item for item in category["nodes"])


def test_summary_matches_membership_routing_and_reward_documents(outputs):
    output_dir, _, leaderboard, summary = outputs
    stored = _json(output_dir / "week_6_summary.json")
    assert stored["membership_distribution"] == leaderboard["membership_distribution"]
    assert sum(stored["membership_distribution"].values()) >= stored["node_count"]
    assert (
        stored["routing_distribution"]["production_assignments"]
        + stored["routing_distribution"]["shadow_assignments"]
        == stored["routing_assignment_count"]
    )
    assert stored["reward_distribution"]["reward_events"] == stored["reward_event_count"]
    assert (
        stored["total_distributed_points"] == "10000.000000"
        and stored["total_undistributed_points"] == "0.000000"
    )
    assert summary.benchmark_passed


def test_markdown_report_has_every_required_section_case_and_refinement(outputs):
    output_dir, _, _, _ = outputs
    report = (output_dir / "week_6_report.md").read_text(encoding="utf-8")
    for section in REQUIRED_REPORT_SECTIONS:
        assert section in report
    for case_id in REQUIRED_CASE_IDS:
        assert f"### {case_id}" in report
    for refinement in DEFERRED_REFINEMENTS:
        assert refinement in report
    assert "Known Limitations" in report
    assert "benchmark-leakage protection" in report


def test_outputs_contain_no_paths_secrets_private_keys_or_raw_poc(outputs):
    output_dir, _, _, _ = outputs
    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in output_dir.iterdir()
    ).lower()
    for forbidden in (
        "/home/",
        "\\users\\",
        "private_key",
        "seed_phrase",
        "mnemonic",
        "wallet_address",
        "poc_content",
        "poc_code",
    ):
        assert forbidden not in combined


def test_repeated_complete_benchmark_outputs_are_logically_identical(tmp_path):
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first = run_week6_subnet_benchmark(first_dir)
    second = run_week6_subnet_benchmark(second_dir)
    assert first[0].model_dump(mode="json") == second[0].model_dump(mode="json")
    assert first[1] == second[1]
    assert first[2].model_dump(mode="json") == second[2].model_dump(mode="json")
